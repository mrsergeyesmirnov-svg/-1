/**
 * Record onboarding HTML reels to higher-quality mute MP4 for Telegram.
 */
import { chromium } from "playwright-core";
import { spawn } from "child_process";
import fs from "fs";
import path from "path";
import { fileURLToPath, pathToFileURL } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const HTML_DIR = path.resolve(
  __dirname,
  "../../restaurant-feedback-bot/onboarding_reels"
);
const OUT_DIR = path.join(HTML_DIR, "mp4");
const TMP_DIR = "/tmp/reel-record-hq";

const W = 390;
const H = 844;
const SCALE = 2;

const REELS = [
  { file: "demo-manager-menu.html", out: "menu.mp4", ms: 11000 },
  { file: "demo-manager-materials.html", out: "mats.mp4", ms: 9500 },
  { file: "demo-manager-report.html", out: "report.mp4", ms: 10500 },
  { file: "demo-manager-signals.html", out: "signals.mp4", ms: 10000 },
  { file: "demo-manager-alert.html", out: "alert.mp4", ms: 12500 },
  { file: "demo-manager-day.html", out: "day.mp4", ms: 10000 },
  { file: "demo-manager-broadcast.html", out: "bcast.mp4", ms: 9000 },
  { file: "demo-manager-close.html", out: "close.mp4", ms: 9500 },
  { file: "demo-manager-stop.html", out: "stop.mp4", ms: 9500 },
  { file: "demo-manager-kitchen.html", out: "kitchen.mp4", ms: 9500 },
  { file: "demo-manager-plan.html", out: "plan.mp4", ms: 9000 },
  { file: "demo-manager-access.html", out: "access.mp4", ms: 10000 },
  { file: "demo-waiter-checkin.html", out: "waiter.mp4", ms: 10500 },
];

function run(cmd, args) {
  return new Promise((resolve, reject) => {
    const p = spawn(cmd, args, { stdio: "inherit" });
    p.on("exit", (code) =>
      code === 0 ? resolve() : reject(new Error(`${cmd} exit ${code}`))
    );
  });
}

async function convert(webm, mp4) {
  // Telegram-friendly H.264, smoother bitrate, no audio
  await run("ffmpeg", [
    "-y",
    "-i",
    webm,
    "-an",
    "-vf",
    `fps=30,scale=${W}:${H}:flags=lanczos`,
    "-c:v",
    "libx264",
    "-pix_fmt",
    "yuv420p",
    "-profile:v",
    "main",
    "-level",
    "3.1",
    "-movflags",
    "+faststart",
    "-crf",
    "18",
    "-preset",
    "slow",
    "-tune",
    "animation",
    mp4,
  ]);
}

fs.mkdirSync(OUT_DIR, { recursive: true });
fs.mkdirSync(TMP_DIR, { recursive: true });

const browser = await chromium.launch({
  executablePath: "/usr/local/bin/google-chrome",
  headless: true,
  args: ["--no-sandbox", "--disable-dev-shm-usage"],
});

for (const reel of REELS) {
  const htmlPath = path.join(HTML_DIR, reel.file);
  if (!fs.existsSync(htmlPath)) {
    console.error("missing", htmlPath);
    continue;
  }
  const outMp4 = path.join(OUT_DIR, reel.out);
  const context = await browser.newContext({
    viewport: { width: W, height: H },
    deviceScaleFactor: SCALE,
    recordVideo: {
      dir: TMP_DIR,
      size: { width: W * SCALE, height: H * SCALE },
    },
  });
  const page = await context.newPage();
  // Slightly soften CSS motion for capture
  await page.addInitScript(() => {
    const s = document.createElement("style");
    s.textContent = `
      * { animation-timing-function: cubic-bezier(0.22,1,0.36,1) !important; }
      .bubble, .btn, .kbtn, .chip, .metric span, .quote, .toast, .cell {
        transition-duration: 0.85s !important;
      }
    `;
    document.documentElement.appendChild(s);
  });
  const url = pathToFileURL(htmlPath).href;
  console.log("recording HQ", reel.file, "→", reel.out);
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForTimeout(400);
  await page.waitForTimeout(reel.ms);
  const video = page.video();
  await context.close();
  const webm = await video.path();
  await convert(webm, outMp4);
  try {
    fs.unlinkSync(webm);
  } catch {}
  const size = fs.statSync(outMp4).size;
  console.log("  ok", reel.out, Math.round(size / 1024), "KB");
}

await browser.close();
console.log("all done →", OUT_DIR);
