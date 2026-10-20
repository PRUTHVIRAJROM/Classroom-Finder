One file per classroom. The file name is the room number: 301.json is room 301.

Open a room's file and put its video address after "source":

  "source": 0                                       your laptop webcam (try 1 if 0 is the wrong camera)
  "source": "rtsp://user:pass@192.168.1.50:554/s1"  a CCTV camera (ask the teacher or IT for the address)
  "source": "C:/Videos/test.mp4"                    a video file (use / instead of \ in paths)
  "source": ""                                      no camera yet: the room shows "No signal"

"detector" is one of:
  "yolo"    RECOMMENDED for CCTV. Counts only PEOPLE (not shadows or curtains), finds
            people sitting still, and checks the picture in small pieces so students far
            away are not missed. The first time it runs it downloads a model file (about
            36 MB) into the models folder. Needs internet once.
  "face"    counts faces looking at the camera (only for a laptop webcam on a desk)
  "person"  older whole-body detector (weak; use "yolo" instead)
  "motion"  counts anything that MOVES. It cannot tell a person from a shadow or a curtain,
            so use it only on a very steady camera, and only if yolo is too slow.

Optional extras in a file:
  "enabled": false    switch this room's camera off without deleting the address
  "max_width": 960    use a sharper picture for far-away people (slower)
  "interval": 2       seconds between counts
  "hold": 30          (motion) keep the highest count of the last 30 seconds, because
                      a student who sits still fades into the background

Extra settings for "detector": "yolo":
  "confidence": 0.3   lower (0.2) finds more far-away students but allows more false
                      ones; higher (0.45) is stricter
  "model": "yolo11n.onnx"   faster and lighter (use this if the computer is slow);
                      "yolo11m.onnx" is more accurate but slower. Default is yolo11s.onnx.
  "tile": false       switch off the piece-by-piece scan (faster, but far students are missed)
  "interval": 3       give a slow computer more time between counts

Extra settings for "detector": "motion":
  "min_area": 80      smallest moving shape that counts, in pixels. Raise it if shadows,
                      curtains or screens get counted; lower it if far students are missed.
  "max_area": 5000    biggest shape that counts (optional)
  "sensitivity": 32   lower = notices smaller changes, higher = ignores more noise

Keep the quotes and commas exactly as they are. If the program says a file is not valid,
compare it with 301.json.

These files hold camera passwords once you add CCTV addresses, so do not share or upload this folder.

New room? Add it to rooms.json, then run:  python run.py --make-camera-files
