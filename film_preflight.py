#!/usr/bin/env python3
"""film-preflight: check a finished video in ONE ffmpeg pass before you publish it.

  python film_preflight.py check film.mp4 [--out DIR] [--every 2] [--cols 6] [--json]
  python film_preflight.py demo                 # makes a 14 s test film with known faults and checks it
  python film_preflight.py bench film.mp4 [-n 3] # one pass vs the same checks run one ffmpeg call at a time

Checks (PASS / WARN / FAIL / INFO): hook motion in 0-2 s, opening on black, black gaps, frozen stretches,
long stills (> 3 s warn, > 10 s fail), duplicate frames, brightness, hard cuts, loudness (EBU R128, true
peak), silences, plus a contact sheet and a spectrogram. Exit 1 if any check FAILs, 2 if the file can't be read.
The ffmpeg filter settings are cinecode's qc.py (MIT) and vibe-knowledge's qa.py (MIT); the PASS/WARN/FAIL
report and exit codes follow win4r/video-director (MIT). Needs only Python 3 and ffmpeg/ffprobe on PATH.
"""
import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FF = ["ffmpeg", "-hide_banner", "-nostdin"]


def call(cmd, cwd=None):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd)
    return r.returncode, r.stdout.replace("\r", ""), r.stderr.replace("\r", "")


def probe(path):
    rc, so, se = call(["ffprobe", "-v", "error", "-count_packets", "-show_format", "-show_streams", "-of", "json", path])
    if rc:
        raise SystemExit(f"film-preflight: cannot read {path}: {se.strip()[-300:]}")
    d = json.loads(so)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise SystemExit(f"film-preflight: no video stream in {path}")
    return {"duration": float(d["format"]["duration"]), "size": int(d["format"].get("size", 0)),
            "width": v["width"], "height": v["height"], "fps": v.get("r_frame_rate"), "frames": int(v.get("nb_read_packets", 0)),
            "vcodec": v.get("codec_name"), "pix_fmt": v.get("pix_fmt"), "audio": bool(a),
            "acodec": a and a.get("codec_name"), "sample_rate": a and a.get("sample_rate")}


def values(text, key):
    return [float(line.split("=")[1]) for line in text.splitlines() if f"{key}=" in line]


def has_drawtext():
    # try it on one tiny frame: some Windows builds list drawtext but crash without a font config
    return call(FF + ["-v", "error", "-f", "lavfi", "-i", "color=s=64x64:d=0.04", "-vf",
                      "drawtext=text='%{pts\:hms}'", "-f", "null", "-"])[0] == 0


def graph(info, every, cols, label):
    n = max(1, math.ceil(info["duration"] / every))
    cols = min(cols, n)
    rows = math.ceil(n / cols)
    lbl = (",drawtext=text='%{pts\\:hms}':x=6:y=6:fontsize=18:fontcolor=white:box=1:boxcolor=black@0.5" if label else "")
    v = ("[0:v]split=7[h][b][f][l][br][c][s];"
         "[h]trim=duration=2,scale=160:-2,format=gray,tblend=all_mode=difference,signalstats,"
         "metadata=print:key=lavfi.signalstats.YAVG:file=hook.txt[ho];"
         "[b]blackdetect=d=0.1:pic_th=0.98:pix_th=0.10[bo];"
         "[f]freezedetect@short=n=-60dB:d=1.5[fo];"
         "[l]fps=10,scale=480:-2,freezedetect@still=n=0.002:d=3[lo];"
         "[br]fps=4,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=bright.txt[bro];"
         "[c]select='gt(scene,0.3)+eq(n,0)',metadata=print:file=cuts.txt[co];"  # frame 0 keeps the output non-empty
         f"[s]select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{every}-0.001)',scale=320:-2{lbl},"
         f"tile={cols}x{rows}:padding=4:margin=4[so]")
    outs = [x for k in ("ho", "bo", "fo", "lo", "bro", "co") for x in ("-map", f"[{k}]", "-f", "null", "-")]
    outs += ["-map", "[so]", "-fps_mode", "vfr", "-frames:v", "1", "-update", "1", "-y", "sheet.jpg",
             "-map", "0:v", "-f", "framemd5", "-y", "frames.md5"]
    if info["audio"]:
        v += (";[0:a]asplit=3[e][si][sp];[e]ebur128=peak=true:framelog=verbose[eo];"
              "[si]silencedetect=n=-50dB:d=0.5[sio];"
              "[sp]showspectrumpic=s=1600x512:scale=log:fscale=log:legend=1[spo]")
        outs += ["-map", "[eo]", "-f", "null", "-", "-map", "[sio]", "-f", "null", "-",
                 "-map", "[spo]", "-frames:v", "1", "-update", "1", "-y", "spectrogram.png"]
    return n, cols, ["-filter_complex", v] + outs


def one_pass(path, out, every=2.0, cols=6):
    """Decode the film once; every check reads its own branch of the filter graph."""
    info = probe(path)
    os.makedirs(out, exist_ok=True)
    label = has_drawtext()
    n, cols, args = graph(info, every, cols, label)
    rc, so, se = call(FF + ["-i", os.path.abspath(path)] + args, cwd=out)
    if rc and label:                      # a Windows ffmpeg can list drawtext but find no font: drop the label
        n, cols, args = graph(info, every, cols, False)
        rc, so, se = call(FF + ["-i", os.path.abspath(path)] + args, cwd=out)
    if rc:
        raise SystemExit(f"film-preflight: ffmpeg failed: {se.strip()[-600:]}")
    rd = lambda f: Path(out, f).read_text(encoding="utf-8", errors="replace") if Path(out, f).exists() else ""
    lines = [f"row {i // cols + 1} col {i % cols + 1}: {int(i * every // 60):02d}:{i * every % 60:05.2f}" for i in range(n)]
    Path(out, "sheet-index.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    cuts = rd("cuts.txt").split("frame:")[2:]   # drop the first record: frame 0, selected only to keep the branch non-empty
    return info, parse(se, rd("hook.txt"), rd("bright.txt"), "frame:" + "frame:".join(cuts) if cuts else "", rd("frames.md5"), info)


def tagged(err, name):
    return "\n".join(l for l in err.splitlines() if f"[{name} @" in l)


def parse(err, hook_txt, bright_txt, cuts_txt, md5_txt, info):
    r = {}
    r["hook"] = values(hook_txt, "YAVG")
    r["black"] = [tuple(float(x) for x in m) for m in
                  re.findall(r"black_start:(\S+) black_end:(\S+) black_duration:(\S+)", err)]
    for key, name in (("freeze", "freezedetect@short"), ("still", "freezedetect@still")):
        sub = tagged(err, name)
        st = [float(x) for x in re.findall(r"freeze_start: ([0-9.]+)", sub)]
        du = [float(x) for x in re.findall(r"freeze_duration: ([0-9.]+)", sub)]
        if len(du) < len(st):             # still frozen when the film ends
            du.append(info["duration"] - st[-1])
        r[key] = list(zip(st, du))
    prev, dups, i = None, [], 0
    for line in md5_txt.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        h = line.strip().split(",")[-1].strip()
        if h == prev:
            dups.append(i)
        prev, i = h, i + 1
    r["frames"], r["dups"] = i, dups
    r["bright"] = values(bright_txt, "YAVG")
    r["cuts"] = [float(x) for x in re.findall(r"pts_time:([0-9.]+)", cuts_txt)]
    summ = err[err.rfind("Summary:"):] if "Summary:" in err else ""
    g = lambda pat: (float(m.group(1)) if (m := re.search(pat, summ)) else None)
    r["loud"] = {"I": g(r"I:\s+(-?[\d.]+) LUFS"), "LRA": g(r"LRA:\s+(-?[\d.]+) LU"), "peak": g(r"Peak:\s+(-?[\d.]+) dBFS")}
    sil = re.findall(r"silence_start: ([0-9.]+)|silence_end: ([0-9.]+) \| silence_duration: ([0-9.]+)", err)
    r["silence"], start = [], None
    for s, e, d in sil:
        if s:
            start = float(s)
        elif start is not None:
            r["silence"].append((start, float(e), float(d)))
            start = None
    if start is not None:
        r["silence"].append((start, info["duration"], info["duration"] - start))
    return r


def lstar(yavg):                         # limited-range luma -> approx. CIE L* (cinecode qc.py)
    y = min(max((yavg - 16) / 219, 0), 1) ** 2.4
    return 116 * y ** (1 / 3) - 16 if y > 0.008856 else 903.3 * y


def hook_stats(v):
    if not v:
        return 0.0, 0.0
    return sum(v) / len(v), min(max(v[i:i + 2]) for i in range(max(1, len(v) - 1)))


def verdicts(info, r, min_mean=0.5):
    """[(status, check, detail)]. FAIL rules are the hard rules of the source projects."""
    out = []
    mean, low = hook_stats(r["hook"])
    ok = bool(r["hook"]) and mean >= min_mean and low >= 0.02
    out.append(("PASS" if ok else "FAIL", "hook", f"0-2 s motion mean {mean:.2f} (>= {min_mean}), weakest pair {low:.3f} (>= 0.02)"))
    first = r["black"][0] if r["black"] else None
    out.append(("FAIL", "black open", f"black from 0.00 to {first[1]:.2f} s") if first and first[0] < 0.05
               else ("PASS", "black open", "first frame is not black"))
    gaps = [b for b in r["black"] if b[0] >= 0.05]
    out.append(("WARN" if gaps else "PASS", "black gaps", ", ".join(f"{s:.2f}-{e:.2f} s" for s, e, _ in gaps) or "none"))
    longest = max((d for _, d in r["still"]), default=0)
    st = "FAIL" if longest > 10 else "WARN" if r["still"] else "PASS"
    out.append((st, "long stills", ", ".join(f"{s:.1f} s for {d:.1f} s" for s, d in r["still"]) or "none over 3 s"))
    out.append(("INFO", "frozen", ", ".join(f"{s:.2f} s for {d:.2f} s" for s, d in r["freeze"]) or "none over 1.5 s"))
    out.append(("INFO", "duplicates", f"{len(r['dups'])} of {r['frames']} frames repeat the one before"))
    if r["bright"]:
        lum = [lstar(v) for v in r["bright"]]
        dark = sum(1 for x in lum if x < 12) / len(lum) * 100
        out.append(("INFO", "brightness", f"mean L* {sum(lum) / len(lum):.1f}, min {min(lum):.1f}, max {max(lum):.1f}, very dark {dark:.1f}%"))
    out.append(("INFO", "cuts", f"{len(r['cuts'])}: " + ", ".join(f"{t:.2f} s" for t in r["cuts"]) if r["cuts"] else "0"))
    if info["audio"]:
        I, P = r["loud"]["I"], r["loud"]["peak"]
        good = I is not None and P is not None and abs(I + 14) <= 1 and P <= -1
        out.append(("PASS" if good else "WARN", "loudness", f"I {I} LUFS (target -14 +/- 1), true peak {P} dBTP (<= -1), LRA {r['loud']['LRA']} LU"))
        out.append(("INFO", "silences", ", ".join(f"{s:.2f}-{e:.2f} s" for s, e, _ in r["silence"]) or "none (< -50 dB, >= 0.5 s)"))
    else:
        out.append(("WARN", "loudness", "no audio stream"))
    return out


def report(path, info, r, v, out):
    head = (f"{path}: {info['width']}x{info['height']} {info['fps']} fps, {info['duration']:.2f} s, {info['frames']} frames, "
            f"{info['vcodec']}/{info['pix_fmt']}, audio {info['acodec'] or 'none'}")
    lines = [head, ""] + [f"{s:<5} {c:<12} {d}" for s, c, d in v]
    lines += ["", f"sheet: {out}/sheet.jpg (index: sheet-index.txt)"] + ([f"spectrogram: {out}/spectrogram.png"] if info["audio"] else [])
    text = "\n".join(lines)
    Path(out, "report.txt").write_text(text + "\n", encoding="utf-8")
    Path(out, "report.json").write_text(json.dumps({"file": str(path), "info": info, "checks": [
        {"status": s, "check": c, "detail": d} for s, c, d in v], "raw": r}, indent=1), encoding="utf-8")
    return text


def check(path, out=None, every=2.0, cols=6, as_json=False):
    if not os.path.isfile(path):
        print(f"film-preflight: no such file: {path}", file=sys.stderr)
        return 2
    out = out or os.path.join(tempfile.gettempdir(), "film-preflight", Path(path).stem)
    info, r = one_pass(path, out, every, cols)
    v = verdicts(info, r)
    text = report(path, info, r, v, out)
    print(Path(out, "report.json").read_text(encoding="utf-8") if as_json else text)
    return 1 if any(s == "FAIL" for s, _, _ in v) else 0


def make_demo(dst):
    """14 s film with known faults: 1 s black at 3 s, a 4 s still at 4 s, a cut at 8 s, 2 s of silence at 5 s."""
    rc, _, se = call(FF + ["-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=3", "-f", "lavfi", "-i", "color=black:s=640x360:r=30:d=1",
        "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=0.04,tpad=stop_mode=clone:stop_duration=4",
        "-f", "lavfi", "-i", "mandelbrot=s=640x360:r=30,trim=duration=6",
        "-f", "lavfi", "-i", "sine=f=440:d=5:sample_rate=48000", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono,atrim=duration=2",
        "-f", "lavfi", "-i", "sine=f=880:d=7:sample_rate=48000",
        "-filter_complex", "[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,format=yuv420p[v];[4:a][5:a][6:a]concat=n=3:v=0:a=1[a]",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-shortest", dst])
    if rc:
        raise SystemExit(f"film-preflight: could not make the demo film: {se.strip()[-300:]}")
    return dst


# ---- the same checks, one ffmpeg call each, with cinecode qc.py's exact arguments (for bench and tests) ----
def separate(path, out):
    os.makedirs(out, exist_ok=True)
    p = os.path.abspath(path)
    ex = lambda c: call(c, cwd=out)
    has_a = bool(ex(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", p])[1].strip())
    dur = float(ex(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p])[1].strip())
    info = {"duration": dur, "audio": has_a}
    ex(["ffprobe", "-v", "error", "-show_entries", "format=duration,size,bit_rate", "-of", "default=nw=1", p])
    ex(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets", "-show_entries",
        "stream=codec_name,profile,width,height,pix_fmt,r_frame_rate,nb_read_packets", "-of", "default=nw=1", p])
    hook_txt = ex(FF + ["-v", "error", "-t", "2", "-i", p, "-vf", "scale=160:-2,format=gray,tblend=all_mode=difference,"
                        "signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-", "-an", "-f", "null", "-"])[1]
    n = max(1, math.ceil(dur / 2)); cols = min(6, n)
    ex(FF + ["-loglevel", "error", "-y", "-i", p, "-vf", "select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,2-0.001)',"
             f"scale=320:-2,tile={cols}x{math.ceil(n / cols)}:padding=4:margin=4", "-fps_mode", "vfr", "-frames:v", "1",
             "-update", "1", "sheet.jpg"])
    _, so, black = ex(FF + ["-i", p, "-vf", "blackdetect=d=0.1:pic_th=0.98:pix_th=0.10", "-an", "-f", "null", "-"])
    _, so, frz = ex(FF + ["-i", p, "-vf", "freezedetect=n=-60dB:d=1.5", "-map", "0:v", "-f", "null", "-"])
    _, so, still = ex(FF + ["-i", p, "-an", "-vf", "fps=10,scale=480:-2,freezedetect=n=0.002:d=3", "-f", "null", "-"])
    md5 = ex(FF + ["-loglevel", "error", "-i", p, "-map", "0:v", "-f", "framemd5", "-"])[1]
    bright = ex(FF + ["-loglevel", "error", "-i", p, "-vf", "fps=4,signalstats,metadata=print:key=lavfi.signalstats.YAVG:file=-",
                      "-an", "-f", "null", "-"])[1]
    cuts = ex(FF + ["-loglevel", "error", "-i", p, "-vf", "select='gt(scene,0.3)',metadata=print:file=-", "-an", "-f", "null", "-"])[1]
    loud = sil = ""
    if has_a:
        loud = ex(FF + ["-i", p, "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])[2]
        sil = ex(FF + ["-i", p, "-af", "silencedetect=n=-50dB:d=0.5", "-vn", "-f", "null", "-"])[2]
        ex(FF + ["-loglevel", "error", "-y", "-i", p, "-lavfi", "showspectrumpic=s=1600x512:scale=log:fscale=log:legend=1",
                 "-frames:v", "1", "-update", "1", "spectrogram.png"])
    # give the two unnamed freezedetect logs the instance names the one-pass graph uses
    frz = re.sub(r"\[\w*freezedetect\w* @", "[freezedetect@short @", frz)
    still = re.sub(r"\[\w*freezedetect\w* @", "[freezedetect@still @", still)
    err = "\n".join([black, frz, still, loud, sil])
    return parse(err, hook_txt, bright, cuts, md5, info)


def bench(path, n=3):
    def timed(fn):
        ts = []
        for _ in range(n):
            with tempfile.TemporaryDirectory() as d:
                t = time.perf_counter(); fn(d); ts.append(time.perf_counter() - t)
        return statistics.median(ts)
    a = timed(lambda d: separate(path, d))
    b = timed(lambda d: one_pass(path, d))
    print(f"{path}: {n} runs each, median wall time")
    print(f"  one ffmpeg call per check (cinecode qc.py style): {a * 1000:.0f} ms")
    print(f"  film-preflight one pass:                          {b * 1000:.0f} ms  ({a / b:.1f}x faster)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="film_preflight.py", description="One-pass QC for a finished video.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check"); c.add_argument("video"); c.add_argument("--out"); c.add_argument("--every", type=float, default=2.0)
    c.add_argument("--cols", type=int, default=6); c.add_argument("--json", action="store_true")
    d = sub.add_parser("demo"); d.add_argument("--out", default="preflight-demo")
    b = sub.add_parser("bench"); b.add_argument("video"); b.add_argument("-n", type=int, default=3)
    a = ap.parse_args(argv)
    if a.cmd == "check":
        return check(a.video, a.out, a.every, a.cols, a.json)
    if a.cmd == "bench":
        return bench(a.video, a.n)
    os.makedirs(a.out, exist_ok=True)
    film = make_demo(os.path.join(a.out, "demo.mp4"))
    rc = check(film, os.path.join(a.out, "qc"))
    print(f"\ndemo film: {film} (made with a black gap at 3 s, a 4 s still and 2 s of silence); check exit code: {rc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
