#!/usr/bin/env python3
"""
apply_patch.py - 套用 unified diff 格式的補丁，不需要系統的 patch 指令。

用法:
    python3 apply_patch.py <patch檔> <要修改的檔案> [更多 patch/檔案配對...]

例:
    python3 apply_patch.py video-rtc.js.patch video-rtc.js
    python3 apply_patch.py webrtc-camera.js.patch webrtc-camera.js

特性:
  - 純標準函式庫, Python 3.6+ 即可 (HA 主機內建)
  - 找不到完全相符的區塊時, 會退而求其次用「忽略行尾空白」比對
  - 全部成功才寫檔; 任一 hunk 失敗就整個不動, 不留半套的檔案
  - 自動先做 .bak 備份
"""

import os
import sys


def parse_patch(text):
    """解析 unified diff, 回傳 [(old_lines, new_lines), ...]"""
    hunks = []
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines):
        if lines[i].startswith('@@'):
            i += 1
            old, new = [], []
            while i < len(lines):
                ln = lines[i]
                if ln.startswith('@@'):
                    break
                if ln.startswith('---') or ln.startswith('+++'):
                    break
                if ln.startswith(' '):
                    old.append(ln[1:]); new.append(ln[1:])
                elif ln.startswith('-'):
                    old.append(ln[1:])
                elif ln.startswith('+'):
                    new.append(ln[1:])
                elif ln.startswith('\\'):
                    pass  # "\ No newline at end of file"
                else:
                    break
                i += 1
            if old or new:
                hunks.append((old, new))
        else:
            i += 1
    return hunks


def find_block(haystack, needle, start):
    """在 haystack 裡找 needle, 回傳起始索引; 找不到回傳 -1。
    先試完全相符, 再試忽略行尾空白。"""
    n = len(needle)
    if n == 0:
        return start

    for pos in range(start, len(haystack) - n + 1):
        if haystack[pos:pos + n] == needle:
            return pos

    stripped = [x.rstrip() for x in needle]
    for pos in range(start, len(haystack) - n + 1):
        if [x.rstrip() for x in haystack[pos:pos + n]] == stripped:
            return pos

    # 也試從頭找 (允許 hunk 順序被調動過)
    for pos in range(0, len(haystack) - n + 1):
        if haystack[pos:pos + n] == needle:
            return pos
    stripped_all = [x.rstrip() for x in haystack]
    for pos in range(0, len(haystack) - n + 1):
        if stripped_all[pos:pos + n] == stripped:
            return pos
    return -1


def apply_one(patch_path, target_path):
    print("=== %s  ->  %s" % (patch_path, target_path))

    if not os.path.exists(patch_path):
        print("  [錯誤] 找不到補丁檔: %s" % patch_path)
        return False
    if not os.path.exists(target_path):
        print("  [錯誤] 找不到目標檔: %s" % target_path)
        print("         請確認 HA 的 custom_components/webrtc/www 目錄路徑")
        return False

    with open(patch_path, encoding='utf-8') as f:
        patch_text = f.read()
    hunks = parse_patch(patch_text)

    if not hunks:
        print("  [錯誤] 補丁檔裡沒有解析到任何 hunk")
        return False

    with open(target_path, encoding='utf-8') as f:
        content = f.read()
    lines = content.splitlines(keepends=True)

    pos = 0
    applied = 0
    for idx, (old, new) in enumerate(hunks, 1):
        at = find_block(lines, old, pos)
        if at < 0:
            print("  [錯誤] hunk %d/%d 找不到相符位置" % (idx, len(hunks)))
            print("         這個檔案的版本跟補丁的基準版本不同")
            print("         檔案未被修改, 請把錯誤訊息回報給提供補丁的人")
            return False
        lines[at:at + len(old)] = new
        pos = at + len(new)
        applied += 1

    # 備份
    bak = target_path + '.bak'
    with open(target_path, encoding='utf-8') as f:
        original = f.read()
    with open(bak, 'w', encoding='utf-8') as f:
        f.write(original)

    with open(target_path, 'w', encoding='utf-8') as f:
        f.write(''.join(lines))

    print("  [成功] 套用 %d/%d 個區塊" % (applied, len(hunks)))
    print("  [備份] %s" % bak)
    return True


def main():
    args = sys.argv[1:]
    if len(args) < 2 or len(args) % 2 != 0:
        print(__doc__)
        return 1

    ok = True
    for i in range(0, len(args), 2):
        if not apply_one(args[i], args[i + 1]):
            ok = False
            break

    if ok:
        print("")
        print("全部套用完成。接下來請:")
        print("  1. HA 設定 -> 系統 -> 儀表板 -> 右上角「資源」-> 刪除 WebRTC Camera")
        print("  2. 瀏覽器按 Ctrl+Shift+R 強制重新載入")
        return 0

    print("")
    print("套用失敗, 沒有任何檔案被修改。")
    return 1


if __name__ == '__main__':
    sys.exit(main())
