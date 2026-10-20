# Put the website online (Render)

**The idea in one picture**

```
 college computer with the camera            Render (free website host)           anyone's phone
 send_camera_to_website.bat  ──── number ───▶  your website  ◀──── opens link ────  sees free rooms
 (counts people, sends only the number)
```

The website lives on Render. The camera stays at college and only sends numbers (like "4 people"). No video leaves your computer.

Render's screens change from time to time, so a button may be named a little differently. The idea stays the same.

## Step 1: Put the new files on GitHub

Extract the update zip into your project folder (choose Replace), then in the VS Code terminal:

```
git add .
git commit -m "add hosting files"
git push
```

## Step 2: Create the website on Render

1. Go to **render.com** and sign up with your GitHub account.
2. Click **New** and choose **Blueprint**.
3. Pick your `classroom-finder` repository. If it is not listed, click the link to give Render access to it.
4. Render reads the file `render.yaml` and shows a service called `classroom-finder` on the free plan. Click **Apply** or **Deploy**.
5. Wait a few minutes until it says **Live**. Click the address at the top. It looks like `https://classroom-finder.onrender.com`.

You should now see your website. Rooms 302 to 319 show "Demo data" (made-up). Room 301 shows "No signal" because no camera is sending yet.

## Step 3: Get the password

1. In Render, open your `classroom-finder` service and click **Environment**.
2. Find `CF_API_KEY` and click the eye icon to reveal it. Copy it. This is the password the camera program uses.

Do not post this password in a chat, in GitHub, or on a slide.

## Step 4: Send room 301's count from your laptop

1. Open `send_camera_to_website.bat` in VS Code and change the first line to your real address: `set SITE=https://classroom-finder.onrender.com`. Save.
2. Double-click `send_camera_to_website.bat`.
3. Paste the password when it asks, then press Enter.
4. Sit in front of the webcam. Within about 10 seconds, room 301 on the live website shows **Free** with "1 person", because one person is fewer than 7.

To see **In use** with just one webcam, lower the limit: in Render, open **Environment**, set `CF_MIN_PEOPLE` to `1`, and wait for the site to restart. Set it back to `7` for the real rule.

Keep that window open. Closing it stops the counts, and room 301 goes back to "No signal" after about 90 seconds. That is on purpose.

## When you get CCTV later

For each room, copy `send_camera_to_website.bat`, and change the `ROOM`, `SOURCE` (the CCTV address) and `DETECTOR` lines. Then add the room number to the `CF_LIVE_ROOMS` setting on Render (Environment tab), separated by commas, like `301,302,305`. Rooms in that list stop showing demo data.

To turn demo data off completely, set `CF_SIMULATE` to `0` on Render.

## Good to know

- **The free plan sleeps** after some minutes without visitors. The first visit can take about a minute to load. The camera program retries on its own.
- **The website is public** to anyone with the link. It shows only free, in use and no signal, with a people count. Ask your teacher before sharing the link widely or using real CCTV.
- **Never put camera passwords on GitHub.** Your `.gitignore` already blocks the camera files. Keep it that way.
- **If it does not work:** open the service in Render and click **Logs** to see the error. Then tell me what it says.
