# Diagnosis and measurements

Taken on 2026-08-03 on the target device described below.

## Starting point

```
Raspberry Pi Zero 2 W Rev 1.0, Debian 12 bookworm, kernel 6.12.34+rpt-rpi-v8
IMX477 at /base/soc/i2c0mux/i2c@1/imx477@1a, modes up to 4056x3040 12-bit
MediaMTX v1.14.0, stream 1440x1080 @ 20 fps H264, 12 Mbit/s
```

No desktop running (`multi-user.target`, lightdm inactive). Processes occupied
only about 50 MB; the apparently high memory usage was page cache plus the CMA
reservation.

## Finding 1: CMA fragmentation

`journalctl -u mediamtx` showed this continuously:

```
encoder_hard_h264_encode(): ioctl(VIDIOC_QBUF) failed
```

Measured rate: **49 errors in 60 seconds** with the stream running.

`dmesg` gives the reason:

```
cma: __cma_alloc: linux,cma: alloc failed, req-size: 4537 pages, ret: -16
cma: number of available pages: 5@1027+112@1040+61@1219+128@1408+185@2119+…
     => 17671 free of 65536 total pages
unicam 3f801000.csi: dma alloc of size 18583552 failed
```

The key detail: 17671 pages free, but badly fragmented, with the largest
contiguous block at 8519 pages. The request was for 4537 pages **in one piece**.
`ret: -16` is `EBUSY`.

Buffer sizes:

| Sensor mode | Buffer (YUV420) |
|---|---|
| 4056×3040 | 18,495,360 B (18.5 MB) |
| 2028×1520 | 4,623,840 B (4.6 MB) |

CMA pool: 256 MB reserved, of which only 22 to 43 MB remained free at runtime.

### Verification

Switched `rpiCameraMode` to `2028:1520:12:P` and restarted the service:

| | Before | After |
|---|---|---|
| QBUF errors | 49 per 60 s | 0 per 20 s, 1 per 50 s |
| CmaFree | 22 MB | 81 to 83 MB |
| HLS playlist | HTTP 200 | HTTP 200 |

The single remaining error most likely comes from the additional HDR buffers and
is harmless.

## Finding 2: the camera is exclusive

Full-resolution still **while MediaMTX is running**:

```
$ rpicam-still -o /tmp/test_full.jpg --width 4056 --height 3040 -n -t 2000
ERROR V4L2 'imx477 10-001a': Unable to set controls: Device or resource busy
INFO Camera camera.cpp:1011 Pipeline handler in use by another process
ERROR: *** failed to acquire camera ***
→ no image
```

Same command **with MediaMTX stopped**:

```
$ sudo systemctl stop mediamtx
$ grep CmaFree /proc/meminfo      → CmaFree: 180128 kB   (was 22 MB)
$ rpicam-still -o /tmp/test_full.jpg --width 4056 --height 3040 -n -t 2000
real  0m6.254s
-rw-r--r-- 1 master master 1.9M /tmp/test_full.jpg     ✓
```

This is what forces the sensor handover design: stills require MediaMTX to
release the sensor. Concurrent access is impossible, regardless of which software
is used.

## Capture timing

Profiling the original 14-second round trip, phase by phase:

| Phase | Before | After |
|---|---|---|
| Stop MediaMTX | 0.2 s | 0.2 s |
| Settle time after the stop | 2.0 s | 0.3 s |
| `rpicam-still` | 3.7 s | 1.9 s |
| Start MediaMTX | 0.3 s | 0.3 s |
| Wait for the stream | 8.5 s | moved off the critical path |

Two thirds of the wall time went into waiting for the stream, long after the
image had been written to disk.

Warm-up time was safe to cut because the microscope light is constant. Mean image
brightness, measured across a downscaled greyscale frame:

| `-t` | Mean brightness |
|---|---|
| 1500 ms | 106.7 |
| 800 ms | 106.7 |
| 500 ms | 106.7 |
| 300 ms | 106.9 |

Result after the changes: 3.3 / 2.8 / 2.7 s across three runs, at an unchanged
4056×3040 and 4.1 MB.

## Thumbnails

The gallery pulled every 4 MB original just to render a preview. Two generation
methods compared on the Zero 2 W:

| Method | Time per image |
|---|---|
| ffmpeg `scale=400:-2` | 8 to 20 s |
| PIL with `draft()` | 1.9 s |

`draft()` lets the JPEG decoder scale while decoding, so a 12 MP frame never has
to be reconstructed at full size.

Gallery load across 8 images:

| | Time | Data |
|---|---|---|
| Originals | 8.8 s | 32.6 MB |
| Thumbnails | 1.4 s | 0.3 MB |

## Network

All stream ports reachable from the LAN (tested from another host):

```
8889/tcp (WebRTC)  OPEN, /cam → HTTP 301 → /cam/
8888/tcp (HLS)     OPEN, /cam/index.m3u8 → HTTP 200
8554/tcp (RTSP)    OPEN, 5 frames read via ffmpeg ✓
```

Temperature 64.5 °C, `vcgencmd get_throttled` = `0x0` (no throttling).

## Survey of existing projects

The question was whether an off-the-shelf project covers this case better.
Answer: **no**, for a structural reason. Every streaming project treats "photo"
as *grab a frame from the stream*. With a 12 MP sensor behind a 1440×1080 stream
that discards roughly 90% of the pixels, which is exactly the wrong trade for
documentation images.

| Project | Assessment |
|---|---|
| **MediaMTX** | Stays the foundation. Native recording and cheap (no re-encoding), control API, rpiCamera source, very actively maintained. |
| picamera2 | Relevant as a library. `still_during_video.py` deliberately uses only half sensor resolution; full resolution requires a mode switch with an interruption ([#841](https://github.com/raspberrypi/picamera2/issues/841), open). |
| go2rtc | Has a real snapshot endpoint, but no native recording and no rpiCamera source. A step backwards here. |
| RPi Cam Web Interface | A good feature match, but a dead legacy stack (RaspiMJPEG); does not run on Bookworm/libcamera. |
| motionEye / Motion | Maintained, but they are surveillance tools; libcamera only via the `libcamerify` shim. Too heavy for 416 MB. |
| camera-streamer | Describes itself as a "draft project". No advantage here. |
| OpenFlexure Server | An interesting picamera2 reference, but built around motor control and too large. |
| MediaMTX web UIs | Admin dashboards without full-resolution snapshots, and Docker-oriented. |

For snapshots the MediaMTX documentation itself points to an ffmpeg workaround,
which also only ever yields stream resolution.
