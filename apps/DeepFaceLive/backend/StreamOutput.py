from enum import IntEnum
from pathlib import Path
from typing import List

import cv2
import numpy as np
from xlib import cv as lib_cv
from xlib import logic as lib_logic
from xlib import os as lib_os
from xlib import time as lib_time
from xlib.image import ImageProcessor
from xlib.mp import csw as lib_csw
from xlib.streamer import FFMPEGStreamer
from xlib.email import SMTPClient

from .BackendBase import (BackendConnection, BackendDB, BackendHost,
                          BackendSignal, BackendWeakHeap, BackendWorker,
                          BackendWorkerState)

class StreamOutputRTSP(BackendHost):
    """
    Bufferizes and streams the output via RTSP.
    """
    def __init__(self, weak_heap: BackendWeakHeap,
                       reemit_frame_signal: BackendSignal,
                       bc_in: BackendConnection,
                       save_default_path: Path = None,
                       backend_db: BackendDB = None):

        super().__init__(backend_db=backend_db,
                         sheet_cls=Sheet,
                         worker_cls=StreamOutputWorker,
                         worker_state_cls=WorkerState,
                         worker_start_args=[weak_heap, reemit_frame_signal, bc_in, save_default_path])

    def get_control_sheet(self) -> 'Sheet.Host':
        return super().get_control_sheet()

class StreamOutputWorker(BackendWorker):
    def get_state(self) -> 'WorkerState':
        return super().get_state()

    def get_control_sheet(self) -> 'Sheet.Worker':
        return super().get_control_sheet()

    def on_start(self, weak_heap: BackendWeakHeap, reemit_frame_signal: BackendSignal,
                       bc_in: BackendConnection, save_default_path: Path):
        self.weak_heap = weak_heap
        self.reemit_frame_signal = reemit_frame_signal
        self.bc_in = bc_in

        self.fps_counter = lib_time.FPSCounter()

        self.buffered_frames = lib_logic.DelayedBuffers()
        self.is_show_window = False

        self.prev_frame_num = -1

        self._wnd_name = 'DeepFaceLive RTSP output'
        self._wnd_showing = False

        self._streamer = FFMPEGStreamer()
        self._smtp_client = None
        self._smtp_last_sent_ts = 0.0

        lib_os.set_timer_resolution(1)

        state, cs = self.get_state(), self.get_control_sheet()

        cs.source_type.call_on_selected(self.on_cs_source_type)
        cs.show_hide_window.call_on_signal(self.on_cs_show_hide_window_signal)
        cs.aligned_face_id.call_on_number(self.on_cs_aligned_face_id)
        cs.target_delay.call_on_number(self.on_cs_target_delay)
        cs.save_sequence_path.call_on_paths(self.on_cs_save_sequence_path)
        cs.save_fill_frame_gap.call_on_flag(self.on_cs_save_fill_frame_gap)
        cs.is_streaming.call_on_flag(self.on_cs_is_streaming)
        cs.stream_addr.call_on_text(self.on_cs_stream_addr)
        cs.stream_port.call_on_number(self.on_cs_stream_port)

        # SMTP hooks
        cs.smtp_enable.call_on_flag(self.on_cs_smtp_enable)
        cs.smtp_host.call_on_text(self.on_cs_smtp_host)
        cs.smtp_port.call_on_number(self.on_cs_smtp_port)
        cs.smtp_username.call_on_text(self.on_cs_smtp_username)
        cs.smtp_password.call_on_text(self.on_cs_smtp_password)
        cs.smtp_use_tls.call_on_flag(self.on_cs_smtp_use_tls)
        cs.smtp_starttls.call_on_flag(self.on_cs_smtp_starttls)
        cs.smtp_from.call_on_text(self.on_cs_smtp_from)
        cs.smtp_to.call_on_text(self.on_cs_smtp_to)
        cs.smtp_subject.call_on_text(self.on_cs_smtp_subject)
        cs.smtp_body.call_on_text(self.on_cs_smtp_body)
        cs.smtp_interval_ms.call_on_number(self.on_cs_smtp_interval_ms)

        cs.source_type.enable()
        cs.source_type.set_choices(SourceType, ViewModeNames, none_choice_name='@misc.menu_select')
        cs.source_type.select(state.source_type)

        cs.target_delay.enable()
        cs.target_delay.set_config(lib_csw.Number.Config(min=0, max=5000, step=100, decimals=0, allow_instant_update=True))
        cs.target_delay.set_number(state.target_delay if state.target_delay is not None else 500)

        cs.avg_fps.enable()
        cs.avg_fps.set_config(lib_csw.Number.Config(min=0, max=240, decimals=1, read_only=True))
        cs.avg_fps.set_number(0)

        cs.show_hide_window.enable()
        self.hide_window()

        if state.is_showing_window is None:
            state.is_showing_window = False

        if state.is_showing_window:
            state.is_showing_window = not state.is_showing_window
            cs.show_hide_window.signal()

        cs.save_sequence_path.enable()
        cs.save_sequence_path.set_config(lib_csw.Paths.Config.Directory('Choose output sequence directory', directory_path=save_default_path))
        cs.save_sequence_path.set_paths(state.sequence_path)

        cs.save_fill_frame_gap.enable()
        cs.save_fill_frame_gap.set_flag(state.save_fill_frame_gap if state.save_fill_frame_gap is not None else True)

        cs.is_streaming.enable()
        cs.is_streaming.set_flag(state.is_streaming if state.is_streaming is not None else False)

        cs.stream_addr.enable()
        cs.stream_addr.set_text(state.stream_addr if state.stream_addr is not None else '127.0.0.1')

        cs.stream_port.enable()
        cs.stream_port.set_config(lib_csw.Number.Config(min=1, max=9999, decimals=0, allow_instant_update=True))
        cs.stream_port.set_number(state.stream_port if state.stream_port is not None else 1234)
        # SMTP defaults and enable controls
        cs.smtp_enable.enable()
        cs.smtp_enable.set_flag(state.smtp_enable if state.smtp_enable is not None else False)

        cs.smtp_host.enable();        cs.smtp_host.set_text(state.smtp_host or '')
        cs.smtp_port.enable();        cs.smtp_port.set_config(lib_csw.Number.Config(min=1, max=65535, decimals=0, allow_instant_update=True)); cs.smtp_port.set_number(state.smtp_port or 465)
        cs.smtp_username.enable();    cs.smtp_username.set_text(state.smtp_username or '')
        cs.smtp_password.enable();    cs.smtp_password.set_text(state.smtp_password or '')
        cs.smtp_use_tls.enable();     cs.smtp_use_tls.set_flag(True if state.smtp_use_tls is None else state.smtp_use_tls)
        cs.smtp_starttls.enable();    cs.smtp_starttls.set_flag(False if state.smtp_starttls is None else state.smtp_starttls)
        cs.smtp_from.enable();        cs.smtp_from.set_text(state.smtp_from or '')
        cs.smtp_to.enable();          cs.smtp_to.set_text(state.smtp_to or '')
        cs.smtp_subject.enable();     cs.smtp_subject.set_text(state.smtp_subject or 'DeepFaceLive Frame')
        cs.smtp_body.enable();        cs.smtp_body.set_text(state.smtp_body or '')
        cs.smtp_interval_ms.enable(); cs.smtp_interval_ms.set_config(lib_csw.Number.Config(min=100, max=3600000, decimals=0, step=100, allow_instant_update=True)); cs.smtp_interval_ms.set_number(state.smtp_interval_ms or 10000)

        self._rebuild_smtp_client()

        # initialize streamer with default udp url unless a full url was provided previously
        if state.stream_addr is not None and '://' in str(state.stream_addr):
            self._streamer.set_output_url(state.stream_addr)
        else:
            self._streamer.set_udp(state.stream_addr or '127.0.0.1', state.stream_port or 1234)

    def on_stop(self):
        self._streamer.stop()

    def on_cs_source_type(self, idx, source_type):
        state, cs = self.get_state(), self.get_control_sheet()
        if source_type in [SourceType.ALIGNED_FACE, SourceType.ALIGNED_N_SWAPPED_FACE]:
            cs.aligned_face_id.enable()
            cs.aligned_face_id.set_config(lib_csw.Number.Config(min=0, max=16, step=1, allow_instant_update=True))
            cs.aligned_face_id.set_number(state.aligned_face_id or 0)
        else:
            cs.aligned_face_id.disable()
        state.source_type = source_type

        self.save_state()
        self.reemit_frame_signal.send()

    def show_window(self):
        state, cs = self.get_state(), self.get_control_sheet()
        cv2.namedWindow(self._wnd_name)
        self._wnd_showing = True

    def hide_window(self):
        state, cs = self.get_state(), self.get_control_sheet()
        if self._wnd_showing:
            cv2.destroyAllWindows()
            self._wnd_showing = False

    def on_cs_show_hide_window_signal(self,):
        state, cs = self.get_state(), self.get_control_sheet()

        state.is_showing_window = not state.is_showing_window
        if state.is_showing_window:
            cv2.namedWindow(self._wnd_name)
        else:
            cv2.destroyAllWindows()
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_aligned_face_id(self, aligned_face_id):
        state, cs = self.get_state(), self.get_control_sheet()
        cfg = cs.aligned_face_id.get_config()
        aligned_face_id = state.aligned_face_id = np.clip(aligned_face_id, cfg.min, cfg.max)
        cs.aligned_face_id.set_number(aligned_face_id)
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_target_delay(self, target_delay):
        state, cs = self.get_state(), self.get_control_sheet()
        cfg = cs.target_delay.get_config()
        target_delay = state.target_delay = int(np.clip(target_delay, cfg.min, cfg.max))
        self.buffered_frames.set_target_delay(target_delay / 1000.0)
        cs.target_delay.set_number(target_delay)
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_save_sequence_path(self, paths: List[Path], prev_paths):
        state, cs = self.get_state(), self.get_control_sheet()
        cs.save_sequence_path_error.set_error(None)
        sequence_path = paths[0] if len(paths) != 0 else None

        if sequence_path is None or sequence_path.exists():
            state.sequence_path = sequence_path
            cs.save_sequence_path.set_paths(sequence_path, block_event=True)
        else:
            cs.save_sequence_path_error.set_error(f'{sequence_path} does not exist.')
            cs.save_sequence_path.set_paths(prev_paths, block_event=True)
        self.save_state()
        self.reemit_frame_signal.send()

    def on_cs_save_fill_frame_gap(self, save_fill_frame_gap):
        state, cs = self.get_state(), self.get_control_sheet()
        state.save_fill_frame_gap = save_fill_frame_gap
        self.save_state()

    def on_cs_is_streaming(self, is_streaming):
        state, cs = self.get_state(), self.get_control_sheet()
        state.is_streaming = is_streaming
        self.save_state()

    def on_cs_stream_addr(self, stream_addr):
        state, cs = self.get_state(), self.get_control_sheet()
        state.stream_addr = stream_addr
        self.save_state()
        # If user supplied a full URL (e.g., srt://host:port?...), use it directly; otherwise treat as UDP host
        if stream_addr is not None and '://' in stream_addr:
            self._streamer.set_output_url(stream_addr)
        else:
            self._streamer.set_udp(state.stream_addr, state.stream_port)

    def on_cs_stream_port(self, stream_port):
        state, cs = self.get_state(), self.get_control_sheet()
        state.stream_port = stream_port
        self.save_state()
        # Changing port only applies to UDP-style settings
        if state.stream_addr is None or '://' not in state.stream_addr:
            self._streamer.set_udp(state.stream_addr or '127.0.0.1', state.stream_port)

    # SMTP handlers
    def _rebuild_smtp_client(self):
        state = self.get_state()
        if not state.smtp_enable:
            self._smtp_client = None
            return
        try:
            self._smtp_client = SMTPClient(host=state.smtp_host or '',
                                           port=state.smtp_port or 465,
                                           username=state.smtp_username or None,
                                           password=state.smtp_password or None,
                                           use_tls=bool(state.smtp_use_tls),
                                           starttls=bool(state.smtp_starttls))
        except Exception:
            self._smtp_client = None

    def on_cs_smtp_enable(self, enable):
        state = self.get_state()
        state.smtp_enable = enable
        self.save_state()
        self._rebuild_smtp_client()

    def on_cs_smtp_host(self, text):
        state = self.get_state(); state.smtp_host = text; self.save_state(); self._rebuild_smtp_client()
    def on_cs_smtp_port(self, number):
        state = self.get_state(); state.smtp_port = int(number); self.save_state(); self._rebuild_smtp_client()
    def on_cs_smtp_username(self, text):
        state = self.get_state(); state.smtp_username = text; self.save_state(); self._rebuild_smtp_client()
    def on_cs_smtp_password(self, text):
        state = self.get_state(); state.smtp_password = text; self.save_state(); self._rebuild_smtp_client()
    def on_cs_smtp_use_tls(self, flag):
        state = self.get_state(); state.smtp_use_tls = bool(flag); self.save_state(); self._rebuild_smtp_client()
    def on_cs_smtp_starttls(self, flag):
        state = self.get_state(); state.smtp_starttls = bool(flag); self.save_state(); self._rebuild_smtp_client()
    def on_cs_smtp_from(self, text):
        state = self.get_state(); state.smtp_from = text; self.save_state()
    def on_cs_smtp_to(self, text):
        state = self.get_state(); state.smtp_to = text; self.save_state()
    def on_cs_smtp_subject(self, text):
        state = self.get_state(); state.smtp_subject = text; self.save_state()
    def on_cs_smtp_body(self, text):
        state = self.get_state(); state.smtp_body = text; self.save_state()
    def on_cs_smtp_interval_ms(self, number):
        state = self.get_state(); state.smtp_interval_ms = int(number); self.save_state()

    def on_tick(self):
        cs, state = self.get_control_sheet(), self.get_state()

        bcd = self.bc_in.read(timeout=0.005)
        if bcd is not None:
            bcd.assign_weak_heap(self.weak_heap)
            cs.avg_fps.set_number(self.fps_counter.step())

            prev_frame_num = self.prev_frame_num
            frame_num = self.prev_frame_num = bcd.get_frame_num()
            if frame_num < prev_frame_num:
                prev_frame_num = self.prev_frame_num = -1

            source_type = state.source_type
            if source_type is not None and \
                (state.is_showing_window or \
                 state.sequence_path is not None or \
                 state.is_streaming):
                buffered_frames = self.buffered_frames

                view_image = None

                if source_type == SourceType.SOURCE_FRAME:
                    view_image = bcd.get_image(bcd.get_frame_image_name())
                elif source_type in [SourceType.MERGED_FRAME, SourceType.MERGED_FRAME_OR_SOURCE_FRAME]:
                    view_image = bcd.get_image(bcd.get_merged_image_name())
                    if view_image is None and source_type == SourceType.MERGED_FRAME_OR_SOURCE_FRAME:
                        view_image = bcd.get_image(bcd.get_frame_image_name())

                elif source_type == SourceType.ALIGNED_FACE:
                    aligned_face_id = state.aligned_face_id
                    for i, fsi in enumerate(bcd.get_face_swap_info_list()):
                        if aligned_face_id == i:
                            view_image = bcd.get_image(fsi.face_align_image_name)
                            break

                elif source_type == SourceType.SWAPPED_FACE:
                    for fsi in bcd.get_face_swap_info_list():
                        view_image = bcd.get_image(fsi.face_swap_image_name)
                        if view_image is not None:
                            break

                elif source_type in [SourceType.SOURCE_N_MERGED_FRAME, SourceType.SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME]:
                    source_frame = bcd.get_image(bcd.get_frame_image_name())
                    if source_frame is not None:
                        source_frame = ImageProcessor(source_frame).to_ufloat32().get_image('HWC')

                    merged_frame = bcd.get_image(bcd.get_merged_image_name())

                    if merged_frame is None and source_type == SourceType.SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME:
                        merged_frame = source_frame

                    if source_frame is not None and merged_frame is not None:
                        view_image = np.concatenate((source_frame, merged_frame), 1)

                elif source_type == SourceType.ALIGNED_N_SWAPPED_FACE:
                    aligned_face_id = state.aligned_face_id
                    aligned_face = None
                    swapped_face = None
                    for i, fsi in enumerate(bcd.get_face_swap_info_list()):
                        if aligned_face_id == i:
                            aligned_face = bcd.get_image(fsi.face_align_image_name)
                            break

                    for fsi in bcd.get_face_swap_info_list():
                        swapped_face = bcd.get_image(fsi.face_swap_image_name)
                        if swapped_face is not None:
                            break

                    if aligned_face is not None and swapped_face is not None:
                        view_image = np.concatenate((aligned_face, swapped_face), 1)

                if view_image is not None:
                    buffered_frames.add_buffer(bcd.get_frame_timestamp(), view_image)

                    if state.sequence_path is not None:
                        img = ImageProcessor(view_image, copy=True).to_uint8().get_image('HWC')

                        file_ext, cv_args = '.jpg', [int(cv2.IMWRITE_JPEG_QUALITY), 100]

                        frame_diff = abs(frame_num - prev_frame_num) if state.save_fill_frame_gap else 1
                        for i in range(frame_diff):
                            n = frame_num - i
                            filename = f'{n:06}'
                            lib_cv.imwrite(state.sequence_path / (filename + file_ext), img, cv_args)

                pr = buffered_frames.process()

                img = pr.new_data
                if img is not None:
                    if state.is_streaming:
                        img = ImageProcessor(view_image).to_uint8().get_image('HWC')
                        self._streamer.push_frame(img)

                    if state.is_showing_window:
                        cv2.imshow(self._wnd_name, img)

                    # SMTP sending at interval
                    if state.smtp_enable and self._smtp_client is not None:
                        now = lib_time.now()
                        interval = max(100, int(state.smtp_interval_ms or 10000)) / 1000.0
                        if now - self._smtp_last_sent_ts >= interval:
                            self._smtp_last_sent_ts = now
                            try:
                                # encode as JPEG
                                jpeg_img = ImageProcessor(img).to_uint8().get_image('HWC')
                                import cv2 as _cv
                                ret, buf = _cv.imencode('.jpg', jpeg_img, [int(_cv.IMWRITE_JPEG_QUALITY), 90])
                                if ret:
                                    to_list = [t.strip() for t in (state.smtp_to or '').split(',') if len(t.strip()) > 0]
                                    if len(to_list) > 0 and (state.smtp_from or ''):
                                        self._smtp_client.send_email(
                                            subject=state.smtp_subject or 'DeepFaceLive Frame',
                                            body=state.smtp_body or '',
                                            from_addr=state.smtp_from,
                                            to_addrs=to_list,
                                            attachments=[('frame.jpg', buf.tobytes(), 'image/jpeg')]
                                        )
                            except Exception:
                                pass

        if state.is_showing_window:
            cv2.waitKey(1)


class SourceType(IntEnum):
    SOURCE_FRAME = 0
    MERGED_FRAME = 1
    MERGED_FRAME_OR_SOURCE_FRAME = 2
    ALIGNED_FACE = 3
    SWAPPED_FACE = 4
    SOURCE_N_MERGED_FRAME = 5
    SOURCE_N_MERGED_FRAME_OR_SOURCE_FRAME = 6
    ALIGNED_N_SWAPPED_FACE = 7

ViewModeNames = [
    '@QStreamOutput.view_mode.source_frame',
    '@QStreamOutput.view_mode.merged_frame',
    '@QStreamOutput.view_mode.merged_or_source_frame',
    '@QStreamOutput.view_mode.aligned_face',
    '@QStreamOutput.view_mode.swapped_face',
    '@QStreamOutput.view_mode.source_and_merged',
    '@QStreamOutput.view_mode.source_and_merged_or_source',
    '@QStreamOutput.view_mode.aligned_and_swapped'
]


class WorkerState(BackendWorkerState):
    source_type : SourceType = None
    aligned_face_id : int = None
    target_delay : int = None
    is_showing_window : bool = None

    sequence_path : Path = None
    save_fill_frame_gap : bool = None

    is_streaming : bool = None
    stream_addr : str = None
    stream_port : int = None

    # SMTP state
    smtp_enable : bool = None
    smtp_host : str = None
    smtp_port : int = None
    smtp_username : str = None
    smtp_password : str = None
    smtp_use_tls : bool = None
    smtp_starttls : bool = None
    smtp_from : str = None
    smtp_to : str = None
    smtp_subject : str = None
    smtp_body : str = None
    smtp_interval_ms : int = None


class Sheet:
    class Host(lib_csw.Sheet.Host):
        def __init__(self):
            super().__init__()
            self.source_type = lib_csw.DynamicSingleSwitch.Client()
            self.show_hide_window = lib_csw.Signal.Client()
            self.aligned_face_id = lib_csw.Number.Client()
            self.target_delay = lib_csw.Number.Client()
            self.avg_fps = lib_csw.Number.Client()

            self.save_sequence_path = lib_csw.Paths.Client()
            self.save_sequence_path_error = lib_csw.Error.Client()
            self.save_fill_frame_gap = lib_csw.Flag.Client()

            self.is_streaming = lib_csw.Flag.Client()
            self.stream_addr = lib_csw.Text.Client()
            self.stream_port = lib_csw.Number.Client()

            # SMTP controls
            self.smtp_enable = lib_csw.Flag.Client()
            self.smtp_host = lib_csw.Text.Client()
            self.smtp_port = lib_csw.Number.Client()
            self.smtp_username = lib_csw.Text.Client()
            self.smtp_password = lib_csw.Text.Client()
            self.smtp_use_tls = lib_csw.Flag.Client()
            self.smtp_starttls = lib_csw.Flag.Client()
            self.smtp_from = lib_csw.Text.Client()
            self.smtp_to = lib_csw.Text.Client()
            self.smtp_subject = lib_csw.Text.Client()
            self.smtp_body = lib_csw.Text.Client()
            self.smtp_interval_ms = lib_csw.Number.Client()

    class Worker(lib_csw.Sheet.Worker):
        def __init__(self):
            super().__init__()
            self.source_type = lib_csw.DynamicSingleSwitch.Host()
            self.show_hide_window = lib_csw.Signal.Host()
            self.aligned_face_id = lib_csw.Number.Host()
            self.target_delay = lib_csw.Number.Host()
            self.avg_fps = lib_csw.Number.Host()

            self.save_sequence_path = lib_csw.Paths.Host()
            self.save_sequence_path_error = lib_csw.Error.Host()
            self.save_fill_frame_gap = lib_csw.Flag.Host()

            self.is_streaming = lib_csw.Flag.Host()
            self.stream_addr = lib_csw.Text.Host()
            self.stream_port = lib_csw.Number.Host()

            # SMTP controls
            self.smtp_enable = lib_csw.Flag.Host()
            self.smtp_host = lib_csw.Text.Host()
            self.smtp_port = lib_csw.Number.Host()
            self.smtp_username = lib_csw.Text.Host()
            self.smtp_password = lib_csw.Text.Host()
            self.smtp_use_tls = lib_csw.Flag.Host()
            self.smtp_starttls = lib_csw.Flag.Host()
            self.smtp_from = lib_csw.Text.Host()
            self.smtp_to = lib_csw.Text.Host()
            self.smtp_subject = lib_csw.Text.Host()
            self.smtp_body = lib_csw.Text.Host()
            self.smtp_interval_ms = lib_csw.Number.Host()


# Preserve public name expected by imports
StreamOutput = StreamOutputRTSP
