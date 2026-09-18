"""Tk diagnostic window for the fixture comparison and the local loopback diagnostics.

    python -m stt_eval ui

Fixture comparison runs `harness.compare`; the loopback mode runs `live.listen`
with the local engine only. The window shows partial and final text per engine,
elapsed times, capture levels and the run status. Cloud transmission of fixtures
is opt-in through the checkbox and applies to that run only.
"""
from __future__ import annotations

import asyncio
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, ttk
from types import SimpleNamespace

from .__main__ import ROOT, make_engines
from .harness import RunLock, compare
from .live import listen
from .safety import safe_error

FONT = 'Yu Gothic UI'
MODE_FIXTURE = 'Fixture比較'
MODE_LOOPBACK = 'PC出力（ローカル）'
ENGINE_TITLES = [('openai', 'Hosted transcription'), ('parakeet', 'NVIDIA Parakeet JA')]


class DiagnosticUI:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue(maxsize=500)
        self.stop_event = threading.Event()
        self.thread = None
        self.root.title('STT comparison diagnostics')
        self.root.geometry('1000x690')
        self.root.minsize(760, 560)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.closing = False
        base = ttk.Frame(root, padding=18)
        base.pack(fill='both', expand=True)
        ttk.Label(base, text='STT 比較診断', font=(FONT, 20, 'bold')).pack(anchor='w')
        ttk.Label(base, text='同一音源 / 日本語精度 / 遅延 / PC負荷', font=(FONT, 10)).pack(anchor='w', pady=(2, 16))
        controls = ttk.Frame(base)
        controls.pack(fill='x')
        self.mode = tk.StringVar(value=MODE_FIXTURE)
        self.engine = tk.StringVar(value='parakeet')
        self.delay = tk.StringVar(value='medium')
        self.duration = tk.StringVar(value='0')
        self.cloud = tk.BooleanVar(value=False)
        self.hints = tk.BooleanVar(value=False)
        choices = [('モード', self.mode, [MODE_FIXTURE, MODE_LOOPBACK]),
                   ('エンジン', self.engine, ['parakeet', 'openai', 'both']),
                   ('hosted遅延設定', self.delay, ['minimal', 'low', 'medium', 'high', 'xhigh'])]
        for title, variable, values in choices:
            frame = ttk.Frame(controls)
            frame.pack(side='left', padx=(0, 14))
            ttk.Label(frame, text=title).pack(anchor='w')
            ttk.Combobox(frame, textvariable=variable, values=values, state='readonly', width=20).pack()
        fixture = ttk.Frame(base)
        fixture.pack(fill='x', pady=12)
        self.manifest = tk.StringVar(value=str(ROOT / 'fixtures' / 'manifests' / 'synthetic-smoke.json'))
        ttk.Entry(fixture, textvariable=self.manifest).pack(side='left', fill='x', expand=True)
        ttk.Button(fixture, text='音源セットを選ぶ', command=self.browse).pack(side='left', padx=(8, 0))
        options = ttk.Frame(base)
        options.pack(fill='x')
        ttk.Checkbutton(options, text='承認済みテスト音源のクラウド送信', variable=self.cloud).pack(side='left')
        ttk.Checkbutton(options, text='固有名詞ヒント', variable=self.hints).pack(side='left', padx=12)
        ttk.Label(options, text='継続試験（分 / 0=1周）').pack(side='left')
        ttk.Entry(options, textvariable=self.duration, width=5).pack(side='left', padx=6)
        action = ttk.Frame(base)
        action.pack(fill='x', pady=14)
        self.start_button = ttk.Button(action, text='▶ Start', command=self.start)
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(action, text='■ Stop', command=self.stop, state='disabled')
        self.stop_button.pack(side='left', padx=8)
        self.status = tk.StringVar(value='待機中 — 初回モデル読込には時間がかかります')
        ttk.Label(action, textvariable=self.status).pack(side='left', padx=10)
        self.progress = ttk.Progressbar(base, mode='indeterminate')
        self.progress.pack(fill='x')
        panes = ttk.Panedwindow(base, orient='horizontal')
        panes.pack(fill='both', expand=True, pady=14)
        self.texts = {}
        self.timings = {}
        for engine, title in ENGINE_TITLES:
            frame = ttk.Frame(panes, padding=10)
            panes.add(frame, weight=1)
            ttk.Label(frame, text=title, font=(FONT, 13, 'bold')).pack(anchor='w')
            timing = tk.StringVar(value='未計測')
            self.timings[engine] = timing
            ttk.Label(frame, textvariable=timing).pack(anchor='w', pady=(2, 8))
            box = tk.Text(frame, wrap='word', font=(FONT, 13), height=10, width=30, state='disabled')
            box.pack(fill='both', expand=True)
            self.texts[engine] = box
        self.meters = tk.StringVar(value='PC出力・マイク: 未取得')
        ttk.Label(base, textvariable=self.meters).pack(anchor='w')
        self.output = tk.StringVar(value='合成音声は動作検証用です。実話者の認識精度を示すものではありません。')
        ttk.Label(base, textvariable=self.output, wraplength=940).pack(anchor='w', pady=(8, 0))
        self.root.after(80, self.poll)

    def browse(self):
        path = filedialog.askopenfilename(title='テストmanifest', filetypes=[('JSON', '*.json')])
        if path:
            self.manifest.set(path)

    def emit(self, event):
        try:
            self.events.put_nowait(event)
        except queue.Full:
            try:
                self.events.get_nowait()
            except queue.Empty:
                pass
            self.events.put_nowait(event)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        try:
            duration = float(self.duration.get())
            if not 0 <= duration <= 70:
                raise ValueError()
        except ValueError:
            self.status.set('継続時間は0〜70分を入力してください')
            return
        live = self.mode.get() == MODE_LOOPBACK
        engine = 'parakeet' if live else self.engine.get()
        if engine in ['openai', 'both'] and not self.cloud.get():
            self.status.set('クラウド送信の承認を選択してください')
            return
        args = SimpleNamespace(engine=engine, allow_cloud_fixtures=self.cloud.get(), budget_usd=5.,
                               hints=self.hints.get(), delay=self.delay.get(), rotation_seconds=3300.,
                               parakeet_interval=.5, precision='fp32')
        manifest = Path(self.manifest.get())
        destination = ROOT / 'artifacts' / datetime.now().strftime('ui-%Y%m%d-%H%M%S')
        self.stop_event.clear()
        self.start_button.configure(state='disabled')
        self.stop_button.configure(state='normal')
        self.status.set('準備中…')
        self.progress.start(15)

        def work():
            try:
                with RunLock(ROOT):
                    engines = make_engines(args, self.emit)
                    if live:
                        seconds = duration * 60 if duration else 600
                        asyncio.run(listen(engines, self.stop_event, self.emit, seconds=seconds))
                    else:
                        asyncio.run(compare(manifest, destination, engines, stop=self.stop_event,
                                            emit=self.emit, duration_s=duration * 60))
                output = str(destination) if not live else '音声・全文transcript保存なし'
                self.emit({'kind': 'done', 'output': output})
            except asyncio.CancelledError:
                self.emit({'kind': 'done', 'output': '初期化中に停止しました'})
            except Exception as error:
                self.emit({'kind': 'failed', 'error': safe_error(error)})

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.status.set('停止処理中…')

    def show_transcript(self, event):
        engine = event['engine']
        box = self.texts[engine]
        box.configure(state='normal')
        box.delete('1.0', 'end')
        box.insert('end', event['text'])
        box.configure(state='disabled')
        label = 'Final' if event['final'] else 'Partial'
        self.timings[engine].set(f"{label} · {event['ms']:.0f} ms")

    def poll(self):
        for _ in range(200):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event['kind']
            if kind == 'transcript':
                self.show_transcript(event)
            elif kind == 'trial':
                self.status.set(f"計測中: {event['test_id']} / {event['condition']}")
            elif kind == 'status':
                self.status.set(f"{event['engine']}: {event['state']}")
            elif kind == 'meter':
                self.meters.set(f"{event['stream']}: RMS {event['rms']:.4f} · ローカル取得")
            elif kind in ['done', 'failed']:
                self.start_button.configure(state='normal')
                self.stop_button.configure(state='disabled')
                self.progress.stop()
                if self.stop_event.is_set():
                    self.status.set('停止しました')
                elif kind == 'done':
                    self.status.set('計測終了')
                else:
                    self.status.set('エラー: ' + event['error'])
                if kind == 'done':
                    self.output.set(event['output'])
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(80, self.poll)

    def close(self):
        if self.thread and self.thread.is_alive():
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    DiagnosticUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
