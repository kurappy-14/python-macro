#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_switch_logs.py

スイッチのバックアップログ (*_backup.log) を一括検証するスクリプト。

概要:
    - カレントディレクトリ（または引数で指定したディレクトリ）の
      ``*_backup.log`` を全て処理する。
    - 各ファイルについて、以下 3 種類のホスト名を比較して
      「可 / 不可 / エラー（該当行なし）」を判定する。
        A: ファイル名ホスト名   （ファイル名から ``_backup.log`` を除いたもの）
        B: プロンプトホスト名   （末尾から探した ``#`` 行の ``#`` 前）
        C: .bat ホスト名        （``ls mc-dir`` 以降に現れる .bat 名の接頭辞）
    - 結果を ``検証.txt`` に UTF-8・上書きで出力する。

標準ライブラリのみを使用する。
"""

import glob
import os
import re
import sys
from pathlib import Path

# 出力ファイル名（実行のたびに上書き）
OUTPUT_FILENAME = "検証.txt"

# 入力ファイルの探索パターン
INPUT_GLOB = "*_backup.log"

# ファイル名末尾のサフィックス（ホスト名抽出時に取り除く）
BACKUP_SUFFIX = "_backup.log"

# 検証範囲の基準となる文字列
MARKER = "ls mc-dir"

# エラー（該当行なし）メッセージ
ERROR_LABEL = "エラー（該当行なし）"


def read_lines(path):
    """ファイルを UTF-8 で読み込み、行リストを返す。

    改行コードは ``\\n`` / ``\\r\\n`` の双方に対応する。
    ファイルサイズは最大 6KB と小さいため、全行をメモリに読み込む。
    """
    # newline="" を指定して改行をそのまま取得し、splitlines() で
    # \n / \r\n の両方を安全に分割する。
    with open(path, "r", encoding="utf-8", newline="") as fp:
        text = fp.read()
    return text.splitlines()


def extract_prompt_host(lines):
    """末尾から逆順に走査し、最初に見つかった ``#`` を含む行から
    プロンプトホスト名（``#`` より前を strip したもの）を返す。

    見つからない場合は ``None`` を返す。
    """
    for line in reversed(lines):
        if "#" in line:
            # 最初の '#' より前を取得（'#' 以降は無視）し、前後空白を除去。
            return line.split("#", 1)[0].strip()
    return None


def find_marker_index(lines):
    """末尾から逆順に走査し、``ls mc-dir`` を含む最後の行のインデックスを返す。

    見つからない場合は ``None`` を返す。
    """
    for idx in range(len(lines) - 1, -1, -1):
        if MARKER in lines[idx]:
            return idx
    return None


def collect_bat_hosts(target_lines, filename_host):
    """検証対象範囲の各行から .bat ファイル名を検出し、
    その接頭辞（.bat ホスト名）を集合で返す。

    パターン: ``{ファイル名ホスト名}_(?:\\d{2}_)?\\d{8}\\.bat``
      - 日付部分は YYYYMMDD（8 桁）のみ許容。
      - 例: tokyo_sw01_00_20260101.bat / tokyo_sw01_20260101.bat
    """
    # ファイル名ホスト名は正規表現メタ文字を含み得るためエスケープする。
    pattern = re.compile(
        re.escape(filename_host) + r"_(?:\d{2}_)?\d{8}\.bat"
    )
    # 内包表記で全行・全マッチを走査し、接頭辞のみを集合に収集（重複除去）。
    return {
        match.split("_")[0]
        for line in target_lines
        for match in pattern.findall(line)
    }


def is_ok(filename_host, prompt_host, bat_hosts):
    """A/B/C を比較し、「可」条件を満たすかどうかを返す。"""
    # ここに来る時点でエラー条件（B 無し / マーカー無し / .bat 無し）は除外済み。
    a = filename_host
    b = prompt_host

    # 可: A==B かつ C が 1 件以上、かつ全ての C が A と一致。
    return a == b and bat_hosts and all(c == a for c in bat_hosts)


def process_file(path):
    """1 ファイルを処理し、(判定区分, 出力行) を返す。"""
    original_name = os.path.basename(path)

    # 2. ファイル名ホスト名（末尾の _backup.log を除いた文字列）
    filename_host = original_name[: -len(BACKUP_SUFFIX)]

    # 3. 全行読み込み（末尾から読むため、後段で reversed / 逆順 index を使用）
    lines = read_lines(path)

    # 4. プロンプトホスト名の検出
    prompt_host = extract_prompt_host(lines)
    if prompt_host is None:
        return "error", f"{original_name} > {ERROR_LABEL}"

    # 5. 検証対象範囲の決定（ls mc-dir を含む最後の行より後ろ）
    marker_index = find_marker_index(lines)
    if marker_index is None:
        return "error", f"{original_name} > {ERROR_LABEL}"
    # 基準行自体は含めず、その後ろ（末尾側）の行を対象範囲とする。
    target_lines = lines[marker_index + 1:]

    # 6. .bat ホスト名の収集
    bat_hosts = collect_bat_hosts(target_lines, filename_host)
    if not bat_hosts:
        return "error", f"{original_name} > {ERROR_LABEL}"

    # 7. 検証・判定
    if is_ok(filename_host, prompt_host, bat_hosts):
        return "ok", f"{original_name} > 可"

    # 不可: A/B/C は揃っているが「可」条件を満たさない。
    # 不一致があっても検出された全ての .bat ホスト名を出力する。
    c_part = ",".join(bat_hosts)
    return "ng", f"{original_name} > 不可 > {filename_host}/{prompt_host}/{c_part}"


def main():
    # 1. 処理対象ディレクトリの決定（第1引数 or カレントディレクトリ）
    target_dir = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()

    # 対象ファイル一覧（ファイル名でソートして安定した順序にする）
    log_files = sorted(glob.glob(os.path.join(target_dir, INPUT_GLOB)))

    # 各ファイルの結果行と、サマリー用カウンタ
    result_lines = []
    counts = {"ok": 0, "ng": 0, "error": 0}

    for path in log_files:
        verdict, line = process_file(path)
        counts[verdict] += 1
        result_lines.append(line)

    # 9. 処理サマリー
    total = len(log_files)
    summary_lines = [
        "",
        "=== 処理サマリー ===",
        f"総ファイル数: {total}",
        f"可: {counts['ok']}",
        f"不可: {counts['ng']}",
        f"{ERROR_LABEL}: {counts['error']}",
    ]

    # 8. 出力（UTF-8・上書き）。サマリーは最末尾に追記。
    output_path = Path(target_dir) / OUTPUT_FILENAME
    with open(output_path, "w", encoding="utf-8") as fp:
        fp.write("\n".join(result_lines + summary_lines) + "\n")


if __name__ == "__main__":
    main()
