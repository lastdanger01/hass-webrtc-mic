#!/usr/bin/env python3
"""
gen_transform.py - 從既有的三份版本綁定 patch，推導出「版本無關的轉換表」。

既有的 patch/video-rtc.js.patch / .v361.patch / .v360.patch 是同一組改動針對
不同上游版本產生的版本。把它們對齊後可以發現：多數 hunk 的 OLD 內容在
三版之間逐行相同 → 可以用「內容錨點」取代行號綁定。

這支腳本輸出 transform_webrtc.py：一份以內容錨點為準、跟上游版本無關的
轉換表。安裝器只要把轉換表套到任何上游版本都成立。

用法:
    python3 gen_transform.py <patch目錄> <輸出檔>
"""

import os
import re
import sys


def parse_hunks(path):
    """解析 unified diff，回傳 [(old_text, new_text), ...]。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines(keepends=True)

    hunks = []
    i = 0
    while i < len(lines):
        if not lines[i].startswith("@@"):
            i += 1
            continue
        i += 1
        old, new = [], []
        while i < len(lines):
            ln = lines[i]
            if ln.startswith("@@"):
                break
            if ln.startswith("---") or ln.startswith("+++"):
                break
            if ln.startswith(" "):
                old.append(ln[1:])
                new.append(ln[1:])
            elif ln.startswith("-"):
                old.append(ln[1:])
            elif ln.startswith("+"):
                new.append(ln[1:])
            elif ln.startswith("\\"):
                pass
            else:
                break
            i += 1
        if old or new:
            hunks.append(("".join(old), "".join(new)))
    return hunks


def load_variants(patch_dir, base):
    """回傳 {variant_name: [hunks]}，variant 為 "" (master) / ".v361" / ".v360"。"""
    out = {}
    for suffix in ("", ".v361", ".v360"):
        path = os.path.join(patch_dir, base + suffix + ".patch")
        if os.path.exists(path):
            out[suffix] = parse_hunks(path)
    return out


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1

    patch_dir = sys.argv[1]
    out_path = sys.argv[2]

    entries = []

    for base, filename in (("video-rtc.js", "video-rtc.js"),
                           ("webrtc-camera.js", "webrtc-camera.js")):
        variants = load_variants(patch_dir, base)
        if not variants:
            raise SystemExit("[錯誤] 找不到 %s 的任何 patch" % base)

        names = list(variants.keys())
        counts = {n: len(variants[n]) for n in names}
        if len(set(counts.values())) != 1:
            raise SystemExit(
                "[錯誤] %s 的各版本 hunk 數不一致：%s" % (base, counts))

        count = counts[names[0]]
        for idx in range(count):
            olds = []
            news = []
            for n in names:
                old, new = variants[n][idx]
                olds.append(old)
                news.append(new)

            # NEW 必須三版一致，否則語意不同，不能合併
            if not (news[0] == news[1] == news[2]):
                raise SystemExit(
                    "[錯誤] %s hunk %d 的 NEW 在各版本不同，語意不一致" % (base, idx + 1))

            # OLD 若一致 → 單一錨點；否則 → 多個候選錨點
            unique_olds = []
            for o in olds:
                if o not in unique_olds:
                    unique_olds.append(o)

            entries.append({
                "file": filename,
                "id": "%s#%d" % (base, idx + 1),
                "olds": unique_olds,
                "new": news[0],
            })

    # 產生 transform_webrtc.py
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(HEADER)
        f.write("\nTRANSFORMS = [\n")
        for e in entries:
            f.write("    {\n")
            f.write("        'file': %r,\n" % e["file"])
            f.write("        'id': %r,\n" % e["id"])
            f.write("        'olds': [\n")
            for o in e["olds"]:
                f.write("            %r,\n" % o)
            f.write("        ],\n")
            f.write("        'new': %r,\n" % e["new"])
            f.write("    },\n")
        f.write("]\n")

    print("  已產生 %s（%d 個轉換）" % (out_path, len(entries)))
    multi = [e for e in entries if len(e["olds"]) > 1]
    print("  其中 %d 個轉換在不同上游版本有不同錨點（已列為候選）" % len(multi))
    for e in multi:
        print("    - %s (%d 個候選)" % (e["id"], len(e["olds"])))
    return 0


HEADER = '''#!/usr/bin/env python3
"""
transform_webrtc.py - 以「內容錨點」套用 webrtc 麥克風對講改動。

這支腳本由 gen_transform.py 產生，請勿手改。

為什麼不用 diff patch？
    原本的做法是每個上游版本各出一份 diff patch。結果是補丁跟上游檔案綁死：
    上游檔只要被改過（哪怕只改一行），patch 就套不進去。實務上這是安裝
    失敗最常見的原因。

這份轉換表以「程式碼內容」當錨點，不看行號，所以任何上游版本都能套。
同一個 hunk 在不同版本若有不同寫法，會列出多個候選錨點。

用法:
    python3 transform_webrtc.py <整合目錄>

整合目錄必須包含:
    <dir>/www/video-rtc.js
    <dir>/www/webrtc-camera.js
    <dir>/__init__.py
    <dir>/manifest.json
"""

import os
import shutil
import sys


'''

if __name__ == "__main__":
    sys.exit(main())
