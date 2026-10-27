"""Create a project-local environment and start the local RFID application."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent
ENV = ROOT / '.venv'
PYTHON = ENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def main():
    if sys.version_info < (3, 10):
        raise RuntimeError('请安装 Python 3.10 或更新版本，再重新启动。')
    os.chdir(ROOT)
    if not PYTHON.exists():
        print('首次启动：正在创建本项目的独立运行环境……', flush=True)
        venv.EnvBuilder(with_pip=True).create(ENV)
    check = subprocess.run([str(PYTHON), '-c', 'import flask, serial'], capture_output=True)
    if check.returncode:
        print('正在安装依赖，需要联网，请稍候……', flush=True)
        subprocess.run([str(PYTHON), '-m', 'pip', 'install', '-r', 'requirements.txt'], check=True)
    with socket.socket() as sock:
        if sock.connect_ex(('127.0.0.1', 6066)) == 0:
            raise RuntimeError('6066 端口已被占用。请先关闭之前的系统启动窗口，再重试。')
    if os.environ.get('RFID_NO_BROWSER') != '1':
        def open_page():
            for _ in range(30):
                time.sleep(1)
                with socket.socket() as sock:
                    sock.settimeout(1)
                    if sock.connect_ex(('127.0.0.1', 6066)) == 0:
                        webbrowser.open('http://127.0.0.1:6066/')
                        return
        threading.Thread(target=open_page, daemon=True).start()
    print('正在启动。请保留此窗口；结束使用时按 Ctrl+C。', flush=True)
    proc = subprocess.Popen([str(PYTHON), 'backend/app.py'])
    try:
        return proc.wait()
    except KeyboardInterrupt:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
        return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        print(f'启动未完成：{exc}\n请检查网络、Python 和使用说明。', file=sys.stderr)
        sys.exit(1)
