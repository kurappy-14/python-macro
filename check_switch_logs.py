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
        C: .dat ホスト名        （``ls mc-dir`` 以降に現れる .dat 名のホスト名部分）
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

# 読み込み時に試行するエンコーディング（先頭から順に試す）。
#   - utf-8-sig : UTF-8（BOM 付き／無し両対応。BOM があれば自動除去）
#   - cp932     : Shift_JIS 系（機器ログで混在することがあるため保険）
#   - euc-jp    : EUC-JP（同上）
# いずれも失敗した場合は最後に errors="replace" で強制的に読み込む。
ENCODING_CANDIDATES = ("utf-8-sig", "cp932", "euc-jp")

# .dat ファイル名を検出する正規表現。
#   - ホスト名: 必ず "Y" で始まり、アンダースコアを含まない（英数字・ハイフン）。
#     → 先頭の "Y" を起点に取得する。.dat ファイル名の前に不要な文字
#       （記号・空白・他の文字列）が付いていても、ホスト名は必ず "Y" で
#       始まる保証があるため、Y を起点にすればホスト名部分のみを抽出できる。
#   - _NN（連番）は任意。日付 \d{8} は YYYYMMDD の 8 桁のみ許容
#     （9 桁以上だと直後の "." が一致せずマッチしない）。
#   - .dat の前後に不要な文字が付き得るため、行頭・行末は固定しない。
#   - 対応する 2 形式: <host>_00_20260101.dat / <host>_20260101.dat
DAT_PATTERN = re.compile(r"(Y[A-Za-z0-9-]*)_(?:\d{2}_)?\d{8}\.dat")


def read_lines(path):
    """ファイルを読み込み、行リストを返す。

    入力は基本 UTF-8 だが、実機ログでは先頭に BOM が付いていたり、
    不正バイトが混入して UnicodeDecodeError になることがある。
    そのため、以下の順で堅牢にデコードする。
      1. utf-8-sig（BOM 付き UTF-8 を含めて優先的に試す）
      2. cp932 / euc-jp（別エンコーディング混在時の保険）
      3. すべて失敗した場合は utf-8 + errors="replace" で強制読み込み
         （検証に使う文字はすべて ASCII のため判定に影響しない）

    改行コードは ``\\n`` / ``\\r\\n`` の双方に対応する。
    ファイルサイズは最大 6KB と小さいため、全バイトをメモリに読み込む。
    """
    # まずバイト列として読み込む（再デコードを試せるようにするため）。
    with open(path, "rb") as fp:
        raw = fp.read()

    # 候補エンコーディングを順に試し、最初に成功したものを採用する。
    for encoding in ENCODING_CANDIDATES:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        # どの候補でもデコードできなかった場合は置換文字で強制デコード。
        text = raw.decode("utf-8", errors="replace")

    # splitlines() で \n / \r\n の両方を安全に分割する。
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


def collect_dat_hosts(target_lines):
    """検証対象範囲の各行から .dat ファイル名を検出し、
    ホスト名部分（.dat ホスト名）を集合で返す。

    パターン:
        ``(Y[A-Za-z0-9-]*)_(?:\\d{2}_)?\\d{8}\\.dat``

    前提:
      - ホスト名はすべて "Y" で始まる。
      - ホスト名にアンダースコアは含まない。
      - 日付部分は YYYYMMDD（8 桁）のみ許容。
      - .dat ファイル名の前後に不要な文字が付く場合があるため、
        行内のどこにあっても検出する。
      - 例: YHOST01_00_20260101.dat / YHOST01_20260101.dat
    """
    # 内包表記で全行・全マッチを走査し、ホスト名グループのみを集合に収集（重複除去）。
    return {
        match.group(1)
        for line in target_lines
        for match in DAT_PATTERN.finditer(line)
    }


def is_ok(filename_host, prompt_host, dat_hosts):
    """A/B/C を比較し、「可」条件を満たすかどうかを返す。"""
    # ここに来る時点でエラー条件（B 無し / マーカー無し / .dat 無し）は除外済み。
    a = filename_host
    b = prompt_host

    # 可: A==B かつ C が 1 件以上、かつ全ての C が A と一致。
    return a == b and dat_hosts and all(c == a for c in dat_hosts)


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

    # 6. .dat ホスト名の収集
    dat_hosts = collect_dat_hosts(target_lines)
    if not dat_hosts:
        return "error", f"{original_name} > {ERROR_LABEL}"

    # 7. 検証・判定
    if is_ok(filename_host, prompt_host, dat_hosts):
        return "ok", f"{original_name} > 可"

    # 不可: A/B/C は揃っているが「可」条件を満たさない。
    # 不一致があっても検出された全ての .dat ホスト名を出力する。
    c_part = ",".join(dat_hosts)
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
