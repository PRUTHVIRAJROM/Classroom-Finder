# Classroom finder (prototype: 3rd floor, rooms 301 to 319)

A website that shows which classrooms are free right now. A camera counts the people in a room, and the website turns that number into **Free**, **Occupied**, or **No signal**.

Only a number (how many people) is stored. Video is never saved.

## Run it on Windows (VS Code)

Open the terminal inside the project folder, then run these one at a time.

**First time only:**

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Every time you want to run it:**

```powershell
.\.venv\Scripts\Activate.ps1
python run.py --simulate --preview
```

Then open these in your browser:

- http://localhost:8000 is the website
- http://localhost:8000/preview is your webcam with green boxes around what it counts

Stop it with `Ctrl+C` in the terminal.

If PowerShell says scripts are disabled, run `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` once, answer Y, and try again.

## What you will see

- **Room 301** uses your laptop webcam. Sit in front of it and the room turns Occupied. Leave, and after 2 minutes it turns Free. For a faster demo, use `python run.py --simulate --preview --free-after 10`.
- **The other 18 rooms** have no camera yet. With `--simulate` they show made-up data, and the website marks them **Demo data**. Without `--simulate` they show **No signal**.
- The face detector counts faces looking at the camera, so it works for a webcam on a desk.

## Camera address for each classroom

The `cameras` folder has one file per classroom: `301.json` is room 301, `302.json` is room 302, and so on up to `319.json`. Open a room's file and put its video address after `"source"`:

```json
{
  "source": 0,
  "detector": "face"
}
```

- `"source": 0` is the laptop webcam (room 301 uses it now). If it picks the wrong camera, try `1`.
- When you get CCTV access, open that room's file and replace the address, for example `"source": "rtsp://username:password@192.168.1.50:554/stream1"`. Ask the teacher or the IT department for each camera's exact address. Nothing else in the project needs to change.
- `"source": ""` means the room has no camera yet, so it shows **No signal** (or **Demo data** with `--simulate`).
- For a CCTV camera that sees people from far away, try `"detector": "person"` and add `"max_width": 960`.
- More options are explained in `cameras/README.txt`.
- These files hold camera passwords once you add CCTV addresses, so do not upload or share the folder.
- Restart the program (`Ctrl+C`, then run it again) after changing a file.

## Changing the rooms

Edit `rooms.json`. Each room needs an `id`, a `building`, and a `floor`. `name` and `capacity` are optional. Then run `python run.py --make-camera-files` to create an empty camera file for any new room, and restart the program.

## If something goes wrong

| What you see | What to do |
|---|---|
| `cannot open video source 0` | Close other apps using the webcam (Zoom, Teams, Camera). In Windows, open Settings, Privacy & security, Camera, and turn on "Let desktop apps access your camera". |
| Room 301 shows No signal | The camera is not readable. Look for a red message in the terminal. |
| Room 301 shows Free when you are sitting there | The face detector needs your face visible and well lit. Check `/preview` to see what it sees. |
| `This OpenCV build has no face detector` | Run `pip install "opencv-python>=4.9,<5"`. OpenCV 5 removed the detectors used here. |

## For the project report

- **Privacy:** the program reads the video, counts, and sends only the number. `/preview` works only on the computer running the program, and nothing is saved.
- **Safety rule:** a dead camera shows "No signal", never "Free".
- **Limits:** counting faces or bodies can be wrong in crowded rooms or bad lighting. Test with real CCTV footage before trusting it.
- **Approval:** get the college's permission before using real classroom CCTV.

## Putting it online

See `DEPLOY.md` for the step-by-step guide (free hosting on Render).

## Extra: phones and other computers

- `python run.py --share` lets phones on the same Wi-Fi open the website at `http://YOUR-PC-IP:8000`. Windows may ask to allow network access; click Allow.
- `python agent/camera_agent.py --help` shows how a separate classroom computer can send its count to the main one.
- Tests: `python -m unittest discover -s tests`
