import numpy as np

from .. import ffmpeg as lib_ffmpeg


class FFMPEGStreamer:
    def __init__(self):
        self._ffmpeg_proc = None
        self._addr = '127.0.0.1'
        self._port = 1234
        self._width = 320
        self._height = 240
        self._output_url = None
        self._force_format = 'mpegts'

    def set_addr_port(self, addr : str, port : int):
        if self._addr != addr or self._port != port:
            self._addr = addr
            self._port = port
            self.stop()
        # reset default url to udp if explicit url not set
        if self._output_url is None:
            self._output_url = f'udp://{self._addr}:{self._port}'

    def set_output_url(self, url: str, force_format: str = 'mpegts'):
        """
        Set full FFmpeg output URL (e.g. 'udp://host:port', 'srt://host:port?mode=caller&latency=200').
        Optionally override the output format (defaults to 'mpegts').
        """
        if self._output_url != url or self._force_format != force_format:
            self._output_url = url
            self._force_format = force_format
            self.stop()

    def set_udp(self, addr: str, port: int):
        self._addr, self._port = addr, port
        self.set_output_url(f'udp://{addr}:{port}', force_format='mpegts')

    def set_srt(self, addr: str, port: int, latency_ms: int = 200, mode: str = 'caller'):
        # pkt_size 1316 is common for mpegts over SRT
        query = f'mode={mode}&latency={max(1, int(latency_ms))}&pkt_size=1316'
        self.set_output_url(f'srt://{addr}:{port}?{query}', force_format='mpegts')
    
    def stop(self):
        if self._ffmpeg_proc is not None:
            self._ffmpeg_proc.kill()
            self._ffmpeg_proc = None
        
    def _restart(self):
        self.stop()
        sink_url = self._output_url or f'udp://{self._addr}:{self._port}'
        args = ['-y', '-re',
                '-f', 'rawvideo',
                '-vcodec','rawvideo',
                '-pix_fmt', 'bgr24',
                '-s', f'{self._width}:{self._height}',
                '-i', '-',
                '-f', self._force_format,
                '-q:v', '2',
                sink_url
        ]
        self._ffmpeg_proc = lib_ffmpeg.run (args, pipe_stdin=True, quiet_stderr=True)#, pipe_stderr=True)

    def push_frame(self, img : np.ndarray):
        H,W,C = img.shape
        if self._width != W or self._height != H:
            self._width = W
            self._height = H
            self.stop()
            
        if self._ffmpeg_proc is None:
            self._restart()
        
        try:
            self._ffmpeg_proc.stdin.write(img)
        except:
            self.stop()
