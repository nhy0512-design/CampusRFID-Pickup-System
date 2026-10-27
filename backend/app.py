"""大学生 RFID 取快递系统 - Flask 后端。

核心业务流：
1. 录入快递时按手机号自动匹配学生 → 写入 packages（pending）
2. 学生第一次刷卡 → 查询 pending 取件码 → 屏幕显示
3. 5 分钟内同卡再刷一次 → 标记全部 pending 为 picked（出门清空）
4. 后续新快递到达 → 又挂到该学生名下 → 下次刷卡又能看到
"""
import json
import os
import queue
import re
import threading
import time
import uuid
from flask import Flask, jsonify, request, Response, stream_with_context, send_file

import database as db
import importer
from rfid_reader import RFIDReader, find_serial_port

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 上传上限 20MB

# ==================== 前端静态托管 ====================
# 以前前端由独立的 `python -m http.server 5173` 提供，页面来自 5173、
# 数据却要跨域请求 6066 —— 浏览器里这条跨域请求常常被代理拦掉/挂死，
# 表现就是「刷卡了网页没反应」。现在让 Flask 自己托管前端，
# 页面和 API 同源，一个端口搞定。
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

_SAFE_STATIC = {"app.js", "index.html", "favicon.ico", "favicon.png", "style.css",
                "vue.global.prod.js"}


@app.route("/")
@app.route("/index.html")
def serve_index():
    return send_file(os.path.join(FRONTEND_DIR, "index.html"))


@app.route("/<path:filename>")
def serve_static(filename):
    # 只放行前端真正用到的静态文件，避免把整个目录暴露出去
    if "/" in filename or ".." in filename or filename not in _SAFE_STATIC:
        return jsonify({"error": "not found"}), 404
    path = os.path.join(FRONTEND_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    return send_file(path)

# ==================== 全局状态 ====================

# 当前读卡器状态（用于前端"设备是否在线"指示）
reader_status = {"status": "starting", "port": None, "error": None, "updated_at": int(time.time())}

# 最新一次刷卡事件（前端轮询 /card/latest 可用）
latest_card_event = None
latest_card_event_lock = threading.Lock()
# 读写器（重）刚连上时置 True：下一次刷卡按「全新一次」处理，不走清空分支
reader_fresh_connection = True

# SSE 订阅者
sse_subscribers: list[queue.Queue] = []
sse_lock = threading.Lock()

# 刷卡状态机参数
#
# TAP_GAP_SECONDS  —— 「抬起检测」阈值。
#   卡片放在读写器上时会被持续读到。只有中途拿开超过这个时长再放回，
#   才认定为「新的一次刷卡」。没有这一步，卡放在读写器上不动会在几秒内
#   被误判成第二次刷卡并直接清空（真机实测踩过这个坑）。
#
# CHECKOUT_WINDOW_SECONDS —— 第一次刷卡（显示取件码）之后，再刷一次
#   完成取件的时间窗口。要覆盖学生「看了取件码去找包裹、拿到再出门」
#   的真实耗时，5 秒远远不够。
TAP_GAP_SECONDS = 2.0
CHECKOUT_WINDOW_SECONDS = 300      # 5 分钟

# 兼容旧代码引用
COOLDOWN_SECONDS = CHECKOUT_WINDOW_SECONDS

# 等待绑卡模式：{student_id: 起始时间}，下一次未知卡到来时自动绑定
awaiting_bind: dict[int, float] = {}
awaiting_bind_lock = threading.Lock()

# 最近读到的「未注册新卡」：{epc: 时间戳}，供网页弹出绑定表单使用
UNKNOWN_CARD_TTL = 600          # 10 分钟内还能绑定
pending_unknown: dict[str, float] = {}
pending_unknown_lock = threading.Lock()

# 导入预览缓存：{token: {"kind":..., "rows":[...]}}
import_cache: dict[str, dict] = {}
import_cache_lock = threading.Lock()


# ==================== 工具函数 ====================

def push_sse(event_type: str, data: dict):
    """向所有 SSE 订阅者推送事件。"""
    payload = {"type": event_type, "ts": int(time.time()), "data": data}
    with sse_lock:
        dead = []
        for q in sse_subscribers:
            try:
                q.put_nowait(payload)
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                sse_subscribers.remove(q)
            except ValueError:
                pass


def handle_rfid_event(evt: dict):
    """读卡器事件回调。"""
    global latest_card_event, reader_status

    if evt.get("type") == "reader_status":
        reader_status.update(
            {
                "status": evt.get("status"),
                "port": evt.get("port"),
                "baudrate": evt.get("baudrate"),
                "error": evt.get("error"),
                "updated_at": int(time.time()),
            }
        )
        # 软硬件刚连上：此时我们不知道卡是不是一直放在读写器上，
        # 所以下一次刷卡一律当作「全新一次」，绝不走清空分支，避免误清别人的快递。
        if evt.get("status") == "connected":
            global reader_fresh_connection
            reader_fresh_connection = True
        push_sse("reader_status", reader_status)
        return

    if evt.get("type") != "epc":
        return

    epc = evt["epc"]
    now = int(time.time())

    # 同一次放置：读卡线程已明确判定这是「卡一直放着」的续读，
    # 什么都不做，也不去刷新 last_seen。
    #
    # 关键：这里以前会调 touch_card_seen()，把 last_seen 一直往后推，
    # 结果下面那道「抬起检测」的基准时间永远是最新的，卡片明明被拿开又放回，
    # 仍被判成「没抬起来过」—— 表现就是第二次刷卡永远不生效、不显示取件完成。
    # 「卡是否还放着」这件事读卡线程的 CARD_LIFT_TIMEOUT 已经判定过了，
    # 上层不该再推算一遍。
    if evt.get("continuous"):
        return

    student = db.get_student_by_epc(epc)

    # 「取件完成」的最短展示时间。
    # 学生刷完第二次后通常还把卡留在读写器上，几秒后读写器会把它当成
    # 一次新的刷卡，立刻把刚出来的「取件完成」顶掉。留一段静默期，
    # 让人有时间看完再走。
    CHECKOUT_HOLD_SECONDS = 15
    with latest_card_event_lock:
        prev = latest_card_event
    if (
        prev
        and prev.get("kind") == "checkout"
        and prev.get("ts")
        and (now - int(prev["ts"])) < CHECKOUT_HOLD_SECONDS
    ):
        return

    # 未知卡
    if not student:
        # 等待绑卡模式？（学生管理页点的"等待刷卡"）
        with awaiting_bind_lock:
            target_id = next((sid for sid, t in awaiting_bind.items() if time.time() - t < 60), None)
            if target_id is not None:
                del awaiting_bind[target_id]
                db.bind_card_to_student(target_id, epc)
                student = db.get_student_by_id(target_id)
                db.log_card_event(epc, student["id"], "bind", f"绑定到 {student['name']}")
                result = {
                    "kind": "bind_success",
                    "epc": epc,
                    "student": student,
                    "message": f"已把卡绑定到 {student['name']}",
                }
                result["ts"] = now
                with latest_card_event_lock:
                    latest_card_event = result
                push_sse("card", result)
                return

        # 普通新卡：挂到"待绑定"列表，网页上直接弹表单让用户填个人信息
        #
        # 注意去重：这张卡没被移开时每个盘点周期（0.4s）都会被读到一次。
        # 如果不去重，它会不停上报 unknown_card，把刚显示出来的取件码
        # 或「取件完成」瞬间刷掉 —— 表现出来就是「刷了卡却没反应」。
        # 这里和已知卡用同样的「一次放置 = 一个事件」逻辑。
        with pending_unknown_lock:
            first_seen = pending_unknown.get(epc, 0)
            still_holding = (time.time() - first_seen) <= TAP_GAP_SECONDS
            pending_unknown[epc] = time.time()
        if still_holding:
            return

        # 大屏保护：刚显示过取件码 / 取件完成的一小段时间内，
        # 不去抢占大屏（常见场景：桌角还放着一张没绑定的卡，
        # 它会不停被读到，把用户刚刷出来的「取件完成」冲掉）。
        # 这种情况下仍然登记到待绑定列表，旁边的绑卡面板照常出现。
        SCREEN_HOLD_SECONDS = 8
        with latest_card_event_lock:
            prev = latest_card_event
        if (
            prev
            and prev.get("kind") in ("show", "checkout")
            and prev.get("ts")
            and (now - int(prev["ts"])) < SCREEN_HOLD_SECONDS
        ):
            return

        db.log_card_event(epc, None, "unknown_card")
        result = {
            "kind": "unknown_card",
            "epc": epc,
            "message": "这是一张新卡，请填写个人信息完成绑定",
        }
        result["ts"] = now
        with latest_card_event_lock:
            latest_card_event = result
        push_sse("card", result)
        return

    state = db.get_card_state(epc)

    # ---- 抬起检测 ----
    # 卡一直放在读写器上时会被反复读到。这里只刷新 last_seen 并直接返回，
    # 不产生任何业务事件，避免「放上去不动就被自动清空」。
    if state and state.get("last_seen") and now - int(state["last_seen"]) <= TAP_GAP_SECONDS:
        db.touch_card_seen(epc)
        return

    # 到这说明卡中途被拿开过，认定为一次独立的刷卡动作
    # 例外：软硬件刚重连时的一读，一律当作新会话，避免误判成「取完了再刷」
    if reader_fresh_connection:
        reader_fresh_connection = False
        is_within_window = False
    else:
        is_within_window = (
            state
            and state.get("state") == "shown"
            and state.get("student_id") == student["id"]
            and (now - int(state.get("shown_at") or 0)) <= CHECKOUT_WINDOW_SECONDS
        )

    if is_within_window:
        # 第二次刷卡 → checkout
        n = db.checkout_all_pending(student["id"], source="checkout")
        db.set_card_state(epc, "idle")
        db.log_card_event(epc, student["id"], "checkout", f"共清空 {n} 件")
        result = {
            "kind": "checkout",
            "epc": epc,
            "student": student,
            "picked_count": n,
            "message": f"{student['name']} 取件完成，已清空 {n} 件",
        }
    else:
        # 第一次刷卡（或间隔已过）→ 显示
        pending = db.pending_for_student(student["id"])
        db.set_card_state(epc, "shown", student_id=student["id"], shown_at=now)
        db.log_card_event(
            epc, student["id"], "show", f"显示 {len(pending)} 件待取"
        )
        result = {
            "kind": "show",
            "epc": epc,
            "student": student,
            "packages": pending,
            "message": (
                f"{student['name']}，你有 {len(pending)} 件待取"
                if pending
                else f"{student['name']}，当前没有待取快递"
            ),
            "cooldown_seconds": COOLDOWN_SECONDS,
        }

    result["ts"] = now
    with latest_card_event_lock:
        latest_card_event = result
    push_sse("card", result)


# ==================== 路由 ====================

# 注意："/" 已经由上面的 serve_index() 提供网页，这里不要再注册同名路由。



@app.route("/api/reader/status")
def api_reader_status():
    info = dict(reader_status)
    if reader is not None:
        try:
            info.update(reader.heartbeat())
        except Exception as e:
            info["heartbeat_error"] = str(e)
    return jsonify(info)


@app.route("/api/card/latest")
def api_card_latest():
    with latest_card_event_lock:
        return jsonify(latest_card_event or {})


@app.route("/api/sse")
def api_sse():
    """SSE 已停用。

    以前网页靠这个长连接接收刷卡事件，但 Flask 自带的开发服务器扛不住长连接：
    跑几十秒后整个服务（连静态文件一起）会被占满、请求全部挂死，
    表现就是「刷第一次卡有反应，再刷就没反应了」。改为网页轮询 /api/card/latest。

    这里保留路由、直接不返回内容，是为了让还缓存着旧代码的浏览器
    不会建立一个永远不释放的长连接。
    """
    return Response(status=204)
    def gen():
        q: queue.Queue = queue.Queue()
        with sse_lock:
            sse_subscribers.append(q)
        try:
            # 推送当前状态作为初始
            yield f"data: {json.dumps({'type': 'reader_status', 'data': reader_status, 'ts': int(time.time())}, ensure_ascii=False)}\n\n"
            with latest_card_event_lock:
                if latest_card_event:
                    yield f"data: {json.dumps({'type': 'card', 'data': latest_card_event, 'ts': int(time.time())}, ensure_ascii=False)}\n\n"
            while True:
                try:
                    payload = q.get(timeout=15)
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # 心跳
                    yield ": heartbeat\n\n"
        finally:
            with sse_lock:
                try:
                    sse_subscribers.remove(q)
                except ValueError:
                    pass

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ============== 学生 ==============

@app.route("/api/students", methods=["GET", "POST"])
def api_students():
    if request.method == "GET":
        kw = request.args.get("q")
        return jsonify(db.list_students(keyword=kw))
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not name or not phone:
        return jsonify({"error": "name 和 phone 必填"}), 400
    sid = db.add_student(
        student_no=(data.get("student_no") or "").strip() or None,
        name=name,
        phone=phone,
        rfid_epc=(data.get("rfid_epc") or "").strip() or None,
    )
    return jsonify(db.get_student_by_id(sid)), 201


@app.route("/api/students/<int:sid>", methods=["GET", "PATCH", "DELETE"])
def api_student_detail(sid):
    s = db.get_student_by_id(sid)
    if not s:
        return jsonify({"error": "not found"}), 404
    if request.method == "GET":
        return jsonify(s)
    if request.method == "PATCH":
        data = request.get_json(force=True) or {}
        db.update_student(sid, **data)
        return jsonify(db.get_student_by_id(sid))
    # DELETE：暂不允许真删（会影响历史记录）
    return jsonify({"error": "暂不支持删除学生，请联系管理员"}), 400


@app.route("/api/students/<int:sid>/bind-card", methods=["POST"])
def api_bind_card(sid):
    """手动把某张 EPC 卡绑定到学生。"""
    data = request.get_json(force=True) or {}
    epc = (data.get("epc") or "").strip()
    if not epc:
        return jsonify({"error": "epc 必填"}), 400
    db.bind_card_to_student(sid, epc)
    return jsonify(db.get_student_by_id(sid))


@app.route("/api/students/<int:sid>/await-bind", methods=["POST", "DELETE"])
def api_await_bind(sid):
    """进入/取消"等待绑卡"模式：下一次未知卡刷卡时自动绑定到该学生。

    60 秒内有效。
    """
    if request.method == "POST":
        with awaiting_bind_lock:
            awaiting_bind[sid] = time.time()
        return jsonify({"ok": True, "message": "请把卡放在读写器上"})
    else:
        with awaiting_bind_lock:
            awaiting_bind.pop(sid, None)
        return jsonify({"ok": True})


# ============== 快递 ==============

@app.route("/api/packages", methods=["GET", "POST"])
def api_packages():
    if request.method == "GET":
        sid = request.args.get("student_id", type=int)
        status = request.args.get("status")
        return jsonify(db.list_packages(student_id=sid, status=status))
    data = request.get_json(force=True) or {}
    phone = (data.get("phone") or "").strip()
    pickup_code = (data.get("pickup_code") or "").strip()
    tracking_no = (data.get("tracking_no") or "").strip()
    if not phone or not pickup_code:
        return jsonify({"error": "phone 和 pickup_code 必填"}), 400
    student = db.get_student_by_phone(phone)
    if not student:
        # 手机号没登记过：默认拒绝并带 need_create 标记，前端提示"是否新建收件人"
        if not data.get("auto_create"):
            return jsonify({
                "error": f"未找到手机号 {phone} 对应的学生",
                "need_create": True,
                "phone": phone,
            }), 400
        if not re.fullmatch(r"1\d{10}", phone):
            return jsonify({"error": "手机号格式不正确，应为 11 位数字"}), 400
        sid = db.add_student(None, f"手机尾号{phone[-4:]}", phone)
        student = db.get_student_by_id(sid)
    pid = db.add_package(tracking_no or None, pickup_code, student["id"])
    return jsonify({**db.list_packages(student_id=student["id"], status="pending")[0], "_new_id": pid}), 201


@app.route("/api/packages/<int:pid>/pick", methods=["POST"])
def api_pick_one(pid):
    """管理端：手工标记某件已取。"""
    now = int(time.time())
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE packages SET status='picked', picked_at=?, picked_event='manual' "
            "WHERE id=? AND status='pending'",
            (now, pid),
        )
    return jsonify({"ok": True})


@app.route("/api/packages/<int:pid>/restore", methods=["POST"])
def api_restore_one(pid):
    """管理端：把已取件恢复为待取（误操作时用）。"""
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE packages SET status='pending', picked_at=NULL, picked_event=NULL WHERE id=?",
            (pid,),
        )
    return jsonify({"ok": True})


# ============== 新卡绑定 ==============

@app.route("/api/unknown-card")
def api_unknown_card():
    """当前等待绑定的新卡（读到的最后一张未注册卡）。"""
    with pending_unknown_lock:
        now = time.time()
        for epc, ts in list(pending_unknown.items()):
            if now - ts > UNKNOWN_CARD_TTL:
                del pending_unknown[epc]
        if not pending_unknown:
            return jsonify({"epc": None})
        epc, ts = max(pending_unknown.items(), key=lambda kv: kv[1])
        return jsonify({"epc": epc, "ts": int(ts)})


def _show_packages(epc: str, student: dict) -> dict:
    """绑卡完成后立刻走一次"显示待取"，让大屏马上出取件码。"""
    now = int(time.time())
    pending = db.pending_for_student(student["id"])
    db.set_card_state(epc, "shown", student_id=student["id"], shown_at=now)
    db.log_card_event(epc, student["id"], "show", f"显示 {len(pending)} 件待取")
    return {
        "kind": "show",
        "epc": epc,
        "student": student,
        "packages": pending,
        "message": (
            f"{student['name']}，你有 {len(pending)} 件待取"
            if pending
            else f"{student['name']}，当前没有待取快递"
        ),
        "cooldown_seconds": COOLDOWN_SECONDS,
        "ts": now,
    }


@app.route("/api/bind-new-card", methods=["POST"])
def api_bind_new_card():
    global latest_card_event
    """给刚读到的新卡绑定个人信息。

    body: {epc, name, phone, student_no?, student_id?}
    - 传 student_id  → 绑定到已有学生
    - 不传           → 按手机号查找学生；找不到就新建学生再绑定
    绑定完成后立即返回该学生的待取快递（大屏直接显示）。
    """
    data = request.get_json(force=True) or {}
    epc = (data.get("epc") or "").strip()
    if not epc:
        return jsonify({"error": "epc 必填"}), 400

    sid = data.get("student_id")
    student = None
    if sid:
        student = db.get_student_by_id(int(sid))

    if student is None:
        phone = importer.clean_phone(str(data.get("phone") or ""))
        name = (data.get("name") or "").strip()
        if not phone:
            return jsonify({"error": "手机号必填"}), 400
        if not re.fullmatch(r"1\d{10}", phone):
            return jsonify({"error": "手机号格式不正确，应为 11 位数字"}), 400
        student = db.get_student_by_phone(phone)
        if student:
            # 已有学生：补充姓名/学号
            patch = {}
            if name and name != student["name"]:
                patch["name"] = name
            sno = (data.get("student_no") or "").strip()
            if sno and sno != (student["student_no"] or ""):
                patch["student_no"] = sno
            if patch:
                db.update_student(student["id"], **patch)
                student = db.get_student_by_id(student["id"])
        else:
            if not name:
                return jsonify({"error": "该手机号尚未登记，请填写姓名"}), 400
            new_id = db.add_student(
                student_no=(data.get("student_no") or "").strip() or None,
                name=name,
                phone=phone,
            )
            student = db.get_student_by_id(new_id)

    db.bind_card_to_student(student["id"], epc)
    student = db.get_student_by_id(student["id"])   # 重新取，带出 rfid_epc
    db.log_card_event(epc, student["id"], "bind", f"绑定到 {student['name']}")
    with pending_unknown_lock:
        pending_unknown.pop(epc, None)

    result = {
        "kind": "bind_success",
        "epc": epc,
        "student": student,
        "message": f"已把卡绑定到 {student['name']}",
        "ts": int(time.time()),
    }
    with latest_card_event_lock:
        latest_card_event = result
    push_sse("card", result)

    # 紧接着显示该学生的待取快递
    shown = _show_packages(epc, student)
    with latest_card_event_lock:
        latest_card_event = shown
    push_sse("card", shown)
    return jsonify({"ok": True, "student": student, "card": shown})


# ============== 文件导入 ==============

@app.route("/api/import/preview", methods=["POST"])
def api_import_preview():
    """上传 CSV/XLSX 文件，解析出预览数据（不写库）。

    form: file=@xxx.xlsx, kind=auto|students|packages
    """
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "请选择文件"}), 400
    try:
        rows, sheets = importer.parse_upload(f.filename, f.read())
    except Exception as e:
        return jsonify({"error": f"文件解析失败：{e}"}), 400
    if not rows:
        return jsonify({"error": "文件里没有数据"}), 400

    mapping = importer.build_mapping(rows)
    kind = (request.form.get("kind") or "auto").strip()
    if kind not in ("students", "packages"):
        kind = importer.guess_kind(mapping)

    records, header_idx = importer.extract_rows(rows, mapping)
    ok, bad = importer.validate(records, kind)

    token = uuid.uuid4().hex[:12]
    with import_cache_lock:
        import_cache[token] = {
            "kind": kind,
            "rows": [r["row"] for r in ok],
            "created_at": time.time(),
        }

    return jsonify({
        "token": token,
        "filename": f.filename,
        "sheets": sheets,
        "kind": kind,
        "mapping": mapping,
        "headers": rows[header_idx] if header_idx < len(rows) else [],
        "total": len(records),
        "valid": len(ok),
        "invalid": len(bad),
        "preview": [r["row"] for r in ok[:20]],
        "errors": bad[:20],
    })


@app.route("/api/import/commit", methods=["POST"])
def api_import_commit():
    """确认导入。body: {"token": "..."}"""
    data = request.get_json(force=True) or {}
    token = (data.get("token") or "").strip()
    with import_cache_lock:
        cached = import_cache.get(token)
    if not cached:
        return jsonify({"error": "预览已过期，请重新上传文件"}), 400

    kind = cached["kind"]
    rows = cached["rows"]
    summary = {
        "kind": kind,
        "total": len(rows),
        "created_students": 0,
        "updated_students": 0,
        "bound_cards": 0,
        "created_packages": 0,
        "skipped": 0,
        "errors": [],
    }

    for i, rec in enumerate(rows):
        line = i + 1
        phone = importer.clean_phone(rec.get("phone", ""))
        if not phone:
            summary["skipped"] += 1
            summary["errors"].append({"line": line, "reason": "缺少手机号"})
            continue

        student = db.get_student_by_phone(phone)
        if student:
            patch = {}
            nm = (rec.get("name") or "").strip()
            sno = (rec.get("student_no") or "").strip()
            if nm and nm != student["name"]:
                patch["name"] = nm
            if sno and sno != (student["student_no"] or ""):
                patch["student_no"] = sno
            if patch:
                db.update_student(student["id"], **patch)
                student = db.get_student_by_id(student["id"])
                summary["updated_students"] += 1
        else:
            nm = (rec.get("name") or "").strip() or f"手机尾号{phone[-4:]}"
            try:
                sid = db.add_student(
                    student_no=(rec.get("student_no") or "").strip() or None,
                    name=nm,
                    phone=phone,
                )
            except Exception as e:
                summary["skipped"] += 1
                summary["errors"].append({"line": line, "reason": f"建学生失败：{e}"})
                continue
            student = db.get_student_by_id(sid)
            summary["created_students"] += 1

        epc = (rec.get("epc") or "").strip()
        if epc:
            db.bind_card_to_student(student["id"], epc)
            summary["bound_cards"] += 1

        if kind == "packages":
            code = (rec.get("pickup_code") or "").strip()
            if not code:
                summary["skipped"] += 1
                summary["errors"].append({"line": line, "reason": "缺少取件码"})
                continue
            if db.has_pending_code(student["id"], code):
                summary["skipped"] += 1
                summary["errors"].append({"line": line, "reason": f"取件码 {code} 已存在"})
                continue
            db.add_package(
                (rec.get("tracking_no") or "").strip() or None,
                code,
                student["id"],
                shelf=(rec.get("shelf") or "").strip() or None,
            )
            summary["created_packages"] += 1

    with import_cache_lock:
        import_cache.pop(token, None)

    push_sse("import_done", summary)
    return jsonify(summary)


@app.route("/api/import/template")
def api_import_template():
    """下载导入模板 CSV。?kind=students|packages"""
    import tempfile
    kind = request.args.get("kind", "students")
    if kind == "packages":
        content = "手机号,取件码,运单号,货架\n13800000001,A-12-8801,DEMO0001,A架-12\n"
        fname = "快递信息导入模板.csv"
    else:
        content = "学号,姓名,手机号,卡号EPC\n2023001,演示同学,13800000001,\n"
        fname = "学生名单导入模板.csv"
    path = os.path.join(tempfile.gettempdir(), fname)
    with open(path, "w", encoding="utf-8-sig", newline="") as fp:
        fp.write(content)
    return send_file(path, as_attachment=True, download_name=fname,
                     mimetype="text/csv")


# ============== 统计 ==============

@app.route("/api/stats")
def api_stats():
    return jsonify(db.get_stats())


@app.route("/api/card-events")
def api_card_events():
    """最近的刷卡事件日志。"""
    limit = request.args.get("limit", default=50, type=int)
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT ce.*, s.name AS student_name "
            "FROM card_events ce LEFT JOIN students s ON s.id=ce.student_id "
            "ORDER BY ce.id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return jsonify([dict(r) for r in rows])


# ============== 开发/调试辅助 ==============

@app.route("/api/debug/simulate", methods=["POST"])
def api_debug_simulate():
    """开发用：模拟一次刷卡事件，便于无硬件环境测试。

    body: {"epc": "...", "student_id": ...}
    """
    if os.environ.get("RFID_ENABLE_SIMULATION") != "1":
        return jsonify({"error": "模拟刷卡默认关闭，请使用真实读卡器"}), 403
    data = request.get_json(force=True) or {}
    epc = (data.get("epc") or "").strip()
    if not epc:
        return jsonify({"error": "epc 必填"}), 400
    handle_rfid_event({"type": "epc", "epc": epc})
    return jsonify({"ok": True})


@app.route("/api/reset", methods=["POST"])
def api_reset():
    """一键复位到「初始状态」：所有快递恢复待取 + 清空刷卡状态 + 大屏回到「请刷卡」。

    演示/换班时用，避免上一次的刷卡结果残留在新会话里。
    """
    global latest_card_event, reader_fresh_connection

    n = db.reset_all_packages_pending()
    with latest_card_event_lock:
        latest_card_event = None
    with pending_unknown_lock:
        pending_unknown.clear()
    with awaiting_bind_lock:
        awaiting_bind.clear()
    reader_fresh_connection = True

    push_sse("card", {"kind": "reset", "message": "已复位"})
    return jsonify({"ok": True, "pending_packages": n})


# ==================== 启动 ====================

# 全局读卡器实例，供 /api/reader/status 读取设备心跳
reader = None


def main():
    global reader

    db.init_db()
    print(f"[boot] DB at {os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'data', 'pickup.db'))}")

    port = find_serial_port()
    if port:
        print(f"[boot] 发现串口 {port}，启动 RFID 后台线程")
    else:
        print("[boot] 未发现 USB 串口，请连接 KL9007U，或检查 USB 串口驱动。")

    reader = RFIDReader(event_callback=handle_rfid_event, poll_interval=0.4)
    reader.start()

    # 同一个 app 再挂到 5173：老习惯的访问地址继续可用，而且同样是同源，
    # 不会再出现「页面在 5173、数据在 6066」的跨域问题。
    try:
        from werkzeug.serving import make_server
        srv = make_server("127.0.0.1", 5173, app, threaded=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        print("[boot] 前端页面已挂载到 http://127.0.0.1:5173 （同源托管）")
    except Exception as e:
        print(f"[boot] 5173 挂载失败（不影响 6066）：{e}")

    print("=" * 50)
    print(" Campus RFID Pickup System")
    print(" API:    http://127.0.0.1:6066")
    print(" 页面:   http://127.0.0.1:5173")
    print(" SSE:    http://127.0.0.1:6066/api/sse")
    print("=" * 50)
    app.run(host="127.0.0.1", port=6066, debug=False, threaded=True)


if __name__ == "__main__":
    main()
