One file per classroom. The file name is the room number: 301.json is room 301.

Open a room's file and put its video address after "source":

  "source": 0                                       your laptop webcam (try 1 if 0 is the wrong camera)
  "source": "rtsp://user:pass@192.168.1.50:554/s1"  a CCTV camera (ask the teacher or IT for the address)
  "source": "C:/Videos/test.mp4"                    a video file (use / instead of \ in paths)
  "source": ""                                      no camera yet: the room shows "No signal"

"detector" is "face" (counts faces looking at the camera, good for a webcam)
or "person" (counts whole bodies, for a camera that sees people from far away).

Optional extras in a file:
  "enabled": false    switch this room's camera off without deleting the address
  "max_width": 960    use a sharper picture for far-away people (slower)
  "interval": 2       seconds between counts

Keep the quotes and commas exactly as they are. If the program says a file is not valid,
compare it with 301.json.

These files hold camera passwords once you add CCTV addresses, so do not share or upload this folder.

New room? Add it to rooms.json, then run:  python run.py --make-camera-files
