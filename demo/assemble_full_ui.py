#!/usr/bin/env python3
"""Assemble the full-UI forge demo: real GitLab screen recordings, sped up
where needed, never cut. Voiceover per scene via adelay."""
import subprocess
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PROD = f"{ROOT}/production"
V = f"{PROD}/video"
A = f"{PROD}/audio"
OUT = f"{PROD}/video/out"
os.makedirs(OUT, exist_ok=True)
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
FONT_R = "/System/Library/Fonts/Supplemental/Arial.ttf"


def run(cmd):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("FAIL: " + cmd[:220] + "\n" + r.stderr[-1500:])


def seg(src, dst, dur, speed, vos, tpad=0.0, offset=0.0):
    """Whole segment (no cuts), uniformly sped up, VO(s) placed by delay."""
    ss = f"-ss {offset} " if offset else ""
    vf = (
        f"[0:v]setpts=PTS/{speed},fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
        "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p"
    )
    if tpad:
        vf += f",tpad=stop_mode=clone:stop_duration={tpad}"
    vf += "[v]"
    audio_in = " ".join(f"-i '{w}'" for w, _ in vos)
    if vos:
        chains = ";".join(
            f"[{i+1}:a]adelay={int(d * 1000)}|{int(d * 1000)},apad=whole_dur={dur}[a{i+1}]"
            for i, (w, d) in enumerate(vos)
        )
        mix = (
            "[a1]anull[a]" if len(vos) == 1
            else "".join(f"[a{i+1}]" for i in range(len(vos)))
            + f"amix=inputs={len(vos)}:normalize=0[a]"
        )
        graph = f"{vf};{chains};{mix}"
        maps = '-map "[v]" -map "[a]" -c:a aac -b:a 160k '
    else:
        graph = vf
        maps = '-map "[v]" '
    run(
        f"ffmpeg -y -loglevel error {ss}-i '{src}' {audio_in} "
        f"-filter_complex \"{graph}\" {maps}"
        f"-t {dur} -c:v libx264 -preset veryfast -crf 19 '{dst}'"
    )


def card(dst, dur, lines, wav=None):
    draw = []
    y = 380
    for size, text in lines:
        esc = text.replace(":", r"\:").replace("'", r"\'").replace(",", r"\,")
        draw.append(f"drawtext=fontfile={FONT}:text='{esc}':fontcolor=white:fontsize={size}:x=(w-text_w)/2:y={y}")
        y += size + 28
    vf = f"color=c=0x0d1117:s=1920x1080:d={dur}," + ",".join(draw) + ",fps=30,format=yuv420p"
    if wav:
        run(f"ffmpeg -y -loglevel error -f lavfi -i \"color=c=0x0d1117:s=1920x1080:d={dur}\" -i '{wav}' "
            f"-filter_complex \"{vf}[v];[1:a]adelay=500|500,apad[a]\" -map \"[v]\" -map \"[a]\" "
            f"-t {dur} -c:v libx264 -preset veryfast -crf 19 -c:a aac -b:a 160k '{dst}'")
    else:
        run(f"ffmpeg -y -loglevel error -f lavfi -i \"color=c=0x0d1117:s=1920x1080:d={dur}\" "
            f"-filter_complex \"{vf}[v]\" -map \"[v]\" -t {dur} -c:v libx264 -preset veryfast -crf 19 '{dst}'")


print("S01 hook card")
card(f"{OUT}/S01.mp4", 12.7,
     [(64, "Self-hosted GitLab."), (44, "No Premium. No Duo. No AI."), (36, "Until now.")],
     wav=f"{A}/S01.wav")

print("S02 repo")
seg(f"{V}/E_repo.webm", f"{OUT}/S02.mp4", dur=18.9, speed=0.85, vos=[(f"{A}/S02.wav", 0.6)])

# S03+S04 over segment A: typing 3x, wait 6x, plan arrival 1x + hold
print("S03/S04 implement -> plan")
run(
    f"ffmpeg -y -loglevel error -i '{V}/A_implement.webm' -i '{A}/S03.wav' -i '{A}/S04.wav' "
    "-filter_complex \""
    "[0:v]trim=0:8,setpts=PTS/3[s1];"
    "[0:v]trim=8:59,setpts=PTS/6[s2];"
    "[0:v]trim=59:64.7,setpts=PTS-STARTPTS[s3];"
    "[s1][s2][s3]concat=n=3:v=1:a=0,fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,"
    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,tpad=stop_mode=clone:stop_duration=6.5,format=yuv420p[v];"
    "[1:a]adelay=800|800,apad=whole_dur=23[a1];"
    "[2:a]adelay=11000|11000,apad=whole_dur=23[a2];"
    "[a1][a2]amix=inputs=2:normalize=0[a]\" "
    '-map "[v]" -map "[a]" -t 23 '
    f"-c:v libx264 -preset veryfast -crf 19 -c:a aac -b:a 160k '{OUT}/S03.mp4'"
)

print("S05 gate")
seg(f"{V}/B_gate.webm", f"{OUT}/S05.mp4", dur=15.2, speed=2.8, vos=[(f"{A}/S05.wav", 0.6)], tpad=2.0)

print("S06 job streaming")
seg(f"{V}/C_job.webm", f"{OUT}/S06.mp4", dur=29.4, speed=3.0, vos=[(f"{A}/S06.wav", 1.0)])

print("S08 fail-safes (red pipeline + repair log)")
seg(f"{V}/C1_red_pipeline.webm", f"{OUT}/S08a.mp4", dur=9.0, speed=1.6, vos=[])
seg(f"{V}/C2_repair.webm", f"{OUT}/S08b.mp4", dur=13.5, speed=1.9, vos=[])
run(
    f"ffmpeg -y -loglevel error -i '{OUT}/S08a.mp4' -i '{OUT}/S08b.mp4' -i '{A}/S08.wav' "
    "-filter_complex \"[0:v][1:v]concat=n=2:v=1:a=0[v];"
    "[2:a]adelay=600|600,apad=whole_dur=22.5[a]\" "
    '-map "[v]" -map "[a]" -t 22.5 '
    f"-c:v libx264 -preset veryfast -crf 19 -c:a aac -b:a 160k '{OUT}/S08.mp4'"
)

print("S07 proof (Draft MR overview, review, evidence)")
seg(f"{V}/D_mr.webm", f"{OUT}/S07.mp4", dur=15.4, speed=1.72, vos=[(f"{A}/S07.wav", 0.8)], tpad=1.0)

print("S09 the deal (back to the merge button)")
seg(f"{V}/D_mr.webm", f"{OUT}/S09.mp4", dur=14.2, speed=0.62, offset=19.0, vos=[(f"{A}/S09.wav", 0.6)])

print("S10 end card")
card(f"{OUT}/S10.mp4", 13.8,
     [(58, "forge"), (34, "Your factory. Your keys. Your merge button.")],
     wav=f"{A}/S10.wav")

print("concat")
names = ["S01", "S02", "S03", "S05", "S06", "S08", "S07", "S09", "S10"]
with open(f"{OUT}/list_ui.txt", "w") as f:
    for name in names:
        f.write(f"file '{OUT}/{name}.mp4'\n")
run(
    f"ffmpeg -y -loglevel error -f concat -safe 0 -i '{OUT}/list_ui.txt' "
    "-c:v libx264 -preset medium -crf 19 -c:a aac -b:a 160k -movflags +faststart "
    f"'{ROOT}/forge-demo.mp4'"
)
dur = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
     "-of", "default=noprint_wrappers=1:nokey=1", f"{ROOT}/forge-demo.mp4"],
    capture_output=True, text=True).stdout.strip()
print(f"DONE: demo/forge-demo.mp4 {dur}s")
