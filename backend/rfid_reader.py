"""恺乐 KL9007U RFID 桌面读写器 —— 串口读取线程。

==================================================================
协议：UHFReader18 User's Manual V2.0（已于 2026-10-15 真机逆向验证）
==================================================================

【关键教训】设备的命令帧 **没有 0xA0 帧头** —— 那是另一种 UHF 协议。
KL9007U 走的是 UHFReader18 帧：

    命令帧 : [Len][Adr][Cmd][Data...][CRC16-LSB][CRC16-MSB]
    响应帧 : [Len][Adr][reCmd][Status][Data...][CRC16-LSB][CRC16-MSB]

    Len    = len(Data) + 4   （命令）
           = len(Data) + 5   （响应）
    Adr    = 读写器地址 0x00~0xFE，0xFF = 广播
    Cmd    = 命令码
    CRC-16 = CCITT reflected, PRESET=0xFFFF, POLY=0x8408, 小端存储

物理层默认 **57600 bps**，但**波特率会被外部工具/固件改动**（2026-10-16 实测已被改成
115200），所以代码里改成启动时自动协商 CANDIDATE_BAUDS，不再写死。
8N1, 无流控。
字节间隔必须小于 15ms，否则同步丢失。

【真机抓到的帧】(2026-10-15)
    Inventory           TX 04 00 01 DB 4B
                        RX 13 00 01 01 01 0C 300833B2DDD9014000000000 8141
    Get Reader Info     TX 04 00 21 D9 6A
                        RX 0D 00 21 00 02 1F D0 02 13 40 17 0A BC F6
    Inventory Single    TX 04 00 0F A5 A2

命令码表：
    0x01 Inventory 盘点多标签        0x21 Get Reader Info 读版本/功率/频段
    0x0F Inventory Single 单标签     0x25 Set Scan Time (n*100ms)
    0x22 Set Region                  0x2F Set Power (dBm)
    0x33 Acousto-optic (LED/蜂鸣)    0x35 Set WorkMode
    0x36 Get WorkMode

状态码：0x00成功 / 0x01盘点完成 / 0xFB场区内无标签 / 0xFE非法命令
"""
import os
import re
import threading
import time
import serial
import serial.tools.list_ports

# ---------- 协议常量 ----------
# 【2026-10-16 更新】波特率会被固件/外部工具改动（已从 57600 变成过 115200），
# 因此不再写死单一值，改为启动时自动协商，见 detect_baudrate()。
BAUDRATE = 115200
CANDIDATE_BAUDS = [115200, 57600, 9600, 19200, 38400, 230400, 460800]
CRC_PRESET = 0xFFFF
CRC_POLY = 0x8408

CMD_INVENTORY = 0x01        # 盘点多标签
CMD_INVENTORY_SINGLE = 0x0F # 单标签盘点
CMD_READ_DATA = 0x02
CMD_WRITE_EPC = 0x04
CMD_GET_INFO = 0x21         # 读读写器信息
CMD_SET_REGION = 0x22
CMD_SET_SCAN_TIME = 0x25
CMD_SET_BAUD = 0x28
CMD_SET_POWER = 0x2F
CMD_BEEP = 0x33             # 声光控制
CMD_SET_WORKMODE = 0x35
CMD_GET_WORKMODE = 0x36

READER_ADDR = 0x00          # 广播可用 0xFF

STATUS_TEXT = {
    0x00: "成功",
    0x01: "盘点完成",
    0x02: "盘点超时溢出",
    0x03: "数据分包",
    0x04: "读写器存储已满",
    0x05: "访问密码错误",
    0xFA: "标签通信失败",
    0xFB: "场区内无标签",
    0xFC: "标签返回错误",
    0xFD: "命令长度错误",
    0xFE: "非法命令/CRC错误",
    0xFF: "参数错误",
}


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT reflected: preset 0xFFFF, poly 0x8408."""
    crc = CRC_PRESET
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ CRC_POLY
            else:
                crc = (crc >> 1)
    return crc & 0xFFFF


def build_frame(cmd: int, data: bytes = b"", addr: int = READER_ADDR) -> bytes:
    """构造命令帧。Len = len(Data) + 4。"""
    ln = len(data) + 4
    raw = bytes([ln, addr, cmd]) + data
    c = crc16_ccitt(raw)
    return raw + bytes([c & 0xFF, (c >> 8) & 0xFF])


def verify_frame(frame: bytes) -> bool:
    """校验整帧 CRC。合法帧对全帧算 CRC 应得 0x0000。"""
    if len(frame) < 5:
        return False
    return crc16_ccitt(frame) == 0


def find_serial_port():
    """自动寻找 CH340 串口（兼容 macOS 原生驱动与 Windows COM 口）。"""
    explicit = os.environ.get("RFID_PORT", "").strip()
    if explicit:
        return explicit
    candidates = []
    for p in serial.tools.list_ports.comports():
        dev = p.device or ""
        desc = (p.description or "").lower()
        hwid = (p.hwid or "").lower()
        low = dev.lower()
        if ("wch" in low or "usbserial" in low or "ch340" in desc
                or "ch341" in desc or "1a86" in hwid):
            candidates.append(dev)
    if not candidates:
        for p in serial.tools.list_ports.comports():
            dev = p.device or ""
            if "bluetooth" not in dev.lower():
                candidates.append(dev)
    # Windows 下优先数字小的 COM 口，Mac 下优先 cu 设备
    candidates.sort(key=lambda dev: (0, int(dev[3:])) if re.fullmatch(r"COM\d+", dev, re.I) else (1 if "/cu." in dev else 2, dev))
    return candidates[0] if candidates else None


def parse_inventory(frame: bytes) -> list[str]:
    """解析盘点响应，返回 EPC hex 字符串列表。

    响应布局:
        [Len][Adr][reCmd=0x01][Status][Num][EPCLen][EPC bytes][AntID?][CRC16 x2]
    """
    epcs = []
    if len(frame) < 7:
        return epcs
    status = frame[3]
    # 无标签 ->空列表即可，不算错误
    if status == 0xFB:
        return epcs
    # 解析 payload：Num 在 Data[0]
    # Data = frame[4 : 1+Len-2]  (Len 不含自身，末尾 2 字节 CRC)
    ln = frame[0]
    end = 1 + ln - 2
    if end > len(frame):
        end = len(frame)
    payload = frame[4:end]
    if not payload:
        return epcs
    num = payload[0]
    i = 1
    for _ in range(num):
        if i >= len(payload):
            break
        epc_len = payload[i]
        i += 1
        if epc_len <= 0 or i + epc_len > len(payload):
            break
        epc = bytes(payload[i:i + epc_len]).hex().upper()
        epcs.append(epc)
        i += epc_len
    # 尾部可能还有 1 字节天线号，忽略
    return epcs


class RFIDReader:
    """后台线程持续盘点，通过回调上报 EPC。

    event_callback 收到 {"type": "epc", "epc": ...}
    以及 {"type": "reader_status", ...}
    """

    # 同一次放置的最大允许间隔：两次读到同一张卡的间隔超过这个值，
    # 就认为卡曾被拿走，下次再读到算新的一次刷卡。
    # 必须明显大于盘点周期（实测约 0.5–0.9s），否则会把一次放置切成多次。
    CARD_LIFT_TIMEOUT = 2.5
    # 标签连续「缺席」多久才认定卡真的被拿走了。
    # 真实使用中读写器会偶发漏读一帧（卡片角度/距离），只看单次间隔会把
    # 「放着不动」误判成取件完成；这里要求连续缺席超过该值才算离开。
    CARD_ABSENT_TIMEOUT = 2.0

    def __init__(self, event_callback=None, poll_interval=0.3, dedup_seconds=None):
        self.event_callback = event_callback
        self.poll_interval = poll_interval
        self._dedup_seconds = dedup_seconds
        self._stop = threading.Event()
        self._thread = None
        self._last_epc = None
        self._last_epc_time = 0.0
        # 设备心跳：最后一次从串口收到任何字节的时间戳（None = 从未收到）
        self.last_rx_at = None
        self.last_card_at = None
        self._absent_since = None
        # 连续多久收不到任何字节就判定掉线并重连
        self.RX_TIMEOUT = 8.0

    def detect_baudrate(self, port, timeout=0.35):
        """用 Get Reader Info 试探设备当前波特率，返回可用的波特率或 None。

        固件波特率可能被其它工具改写，写死一个值会导致"端口连上了但完全静默"，
        所以每次连接前都协商一次。
        """
        cmd = build_frame(CMD_GET_INFO)
        for baud in CANDIDATE_BAUDS:
            try:
                ser = serial.Serial(port, baudrate=baud, timeout=timeout, write_timeout=1)
            except Exception:
                continue
            try:
                ser.reset_input_buffer()
                ser.write(cmd)
                time.sleep(0.2)
                rx = ser.read(256)
                if rx and verify_frame(rx) and len(rx) >= 3 and rx[2] == CMD_GET_INFO:
                    print(f"[rfid] 波特率协商成功: {baud} bps")
                    return baud
            except Exception:
                pass
            finally:
                try:
                    ser.close()
                except Exception:
                    pass
        return None

    def heartbeat(self):
        """给上层轮询用的健康摘要。"""
        now = time.time()
        return {
            "last_rx_at": self.last_rx_at,
            "rx_age": None if self.last_rx_at is None else round(now - self.last_rx_at, 1),
            "last_card_at": self.last_card_at,
            # 实时在场指示：3 秒内还读到就算卡还放在读写器上
            "card_present": bool(
                self.last_card_at and (now - self.last_card_at) < 3),
        }

    def _emit(self, evt):
        if self.event_callback:
            try:
                self.event_callback(evt)
            except Exception:
                pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="rfid-reader")
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            port = find_serial_port()
            if not port:
                self._emit({"type": "reader_status", "status": "no_port"})
                time.sleep(3)
                continue
            baud = self.detect_baudrate(port)
            if baud is None:
                self._emit({"type": "reader_status", "status": "silent",
                            "port": port,
                            "error": "端口已打开但读写器无任何回应，请检查供电/USB/天线"})
                time.sleep(3)
                continue
            try:
                ser = serial.Serial(
                    port,
                    baudrate=baud,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.4,
                    write_timeout=1,
                )
                self._emit({"type": "reader_status", "status": "connected",
                            "port": port, "baudrate": baud})
                try:
                    self._read_loop(ser)
                finally:
                    ser.close()
            except Exception as e:
                self._emit({"type": "reader_status", "status": "error",
                            "error": str(e)})
                time.sleep(2)

    def _read_loop(self, ser):
        """持续盘点。

        「一次放置 = 一个事件」：卡放在读写器上会被反复读到，这里只在
        卡片**新出现**时上报一次 EPC；只要两次读到同一张卡的间隔没超过
        CARD_LIFT_TIMEOUT，就认为是同一次放置，只刷新时间戳不再上报。
        这样上层状态机拿到的就是干净的一次次「刷卡」动作。
        """
        self.handshake(ser)
        inv_cmd = build_frame(CMD_INVENTORY)
        # 自愈：每轮盘点设备都应回帧，连续 RX_TIMEOUT 秒没收到任何字节就认为
        # 掉线/波特率漂移，抛出去让外层重新协商重连。
        self.last_rx_at = time.time()

        while not self._stop.is_set():
            if time.time() - (self.last_rx_at or 0) > self.RX_TIMEOUT:
                raise RuntimeError(
                    f"读写器连续 {self.RX_TIMEOUT} 秒无响应，尝试重新连接")

            try:
                ser.reset_input_buffer()
                ser.write(inv_cmd)
                time.sleep(0.12)
                buf = ser.read(512)
            except Exception:
                raise

            if buf:
                # 读写器每轮盘点都会回帧（没卡时是空盘点帧），收到字节就说明硬件在线
                self.last_rx_at = time.time()

            epcs = parse_inventory(buf)
            now = time.time()

            if epcs:
                # 卡片在场：清掉缺席计时
                self._absent_since = None
                self.last_card_at = now
            else:
                # 本轮没读到任何标签。真实使用中有零星漏读（卡片角度/距离），
                # 只有「连续缺席」超过 CARD_ABSENT_TIMEOUT 才认定卡被拿走。
                if self._last_epc is not None:
                    if self._absent_since is None:
                        self._absent_since = now
                    elif now - self._absent_since > self.CARD_ABSENT_TIMEOUT:
                        self._last_epc = None
                        self._absent_since = None

            for epc in epcs:
                # 同一张卡、且距离上次读到不超过 CARD_LIFT_TIMEOUT → 属于「同一次放置」。
                # 不能只靠上层用时间差反推 —— 轮询一轮要近 1 秒，窗口很容易抖动跨过，
                # 导致「放着不动」被误判成取件完成。由读卡线程显式打 continuous 标记。
                continuous = (
                    epc == self._last_epc
                    and (now - self._last_epc_time) < self.CARD_LIFT_TIMEOUT
                )
                self._last_epc = epc
                self._last_epc_time = now
                self._emit({"type": "epc", "epc": epc, "continuous": continuous})

            time.sleep(self.poll_interval)

    def handshake(self, ser):
        """握手：读一次读写器信息并确认波特率正确。失败静默，不阻塞主循环。"""
        try:
            ser.reset_input_buffer()
            ser.write(build_frame(CMD_GET_INFO))
            time.sleep(0.2)
            rx = ser.read(256)
            if rx and verify_frame(rx):
                print("[rfid] 握手成功，读写器在线:", rx.hex(' ').upper())
            elif rx:
                print("[rfid] 握手收到数据但 CRC 异常:", rx.hex(' ').upper())
        except Exception as e:
            print("[rfid] 握手失败:", e)


if __name__ == "__main__":
    def cb(evt):
        print("[event]", evt)

    print("KL9007U 读卡测试 —— 请把卡放到读写器上")
    print("协议: UHFReader18 @57600bps")
    port = find_serial_port()
    print("发现串口:", port)
    if port:
        r = RFIDReader(event_callback=cb)
        r.start()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            r.stop()
