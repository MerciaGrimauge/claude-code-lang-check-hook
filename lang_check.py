# Claude の、ユーザーに見える文が英語に崩れていないかを調べるフック。
# 使い方: lang_check.py stop | tool （標準入力にフックの JSON）
# - stop: 応答の最後の文が英語なら、差し戻して日本語で書き直させる（同じ応答では 1 回だけ）。
# - tool: ツールを呼んだ後、直前の途中経過の文が英語なら、Claude に注意を返す（作業は止めない）。
# 英語かどうか: コードブロック・バッククォートの中・URL・パスを除き、英字 ÷（英字 + 日本語の文字）が
# 0.8 を超え、数えた文字が 40 以上のとき。
# 例外（フックは何も返さない）:
# - 拒否・安全上の応答らしい文（REFUSAL に一致）: 英語→日本語化の命令→英語の拒否、の繰り返しを防ぐ。
# - 同じセッションで、前回の発火から COOLDOWN_SEC 秒以内の再発火。
import json
import os
import re
import sys
import time

THRESHOLD = 0.8
MIN_CHARS = 40
COOLDOWN_SEC = 30
STATE_DIR = os.environ.get("LANG_CHECK_STATE_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "state")

FENCE = re.compile(r"```.*?(```|$)", re.S)
INLINE = re.compile(r"`[^`\n]*`")
URL = re.compile(r"https?://\S+|claude://\S+")
LINK_TARGET = re.compile(r"\]\([^)]*\)")
PATH = re.compile(r"(?:[A-Za-z]:[\\/]|[\\/]{1,2}|~[\\/])[^\s、。）)」]*|\b[\w.-]+[\\/][\w.\\/-]+")
JP = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
EN = re.compile(r"[A-Za-z]")
_ACT = (r"(?:help|assist|comply|fulfil+|provide|continue|proceed|support|generate|create|write|produce"
        r"|do (?:that|this)|with (?:this|that|your|the))")
REFUSAL = re.compile(
    r"\bI\s*(?:can['’]?t|cannot|can\s+not|won['’]?t|will\s+not|am\s+unable\s+to|['’]m\s+unable\s+to"
    r"|am\s+not\s+able\s+to|['’]m\s+not\s+able\s+to|must\s+decline)\s+(?:\w+\s+){0,3}?" + _ACT
    + r"|\bunable\s+to\s+(?:help|assist|comply|continue|proceed)"
    + r"|\b(?:not\s+able|can['’]?t|cannot)\s+to?\s*(?:help|assist|comply)"
    + r"|safety\s+(?:classifier|polic(?:y|ies)|guidelines|reasons|concerns)"
    + r"|against\s+(?:my|our)\s+(?:guidelines|polic(?:y|ies))"
    + r"|\b(?:I['’]m|I\s+am)\s+sorry,?\s+but\b|\bSorry,?\s+but\s+I\b|\bdecline\s+to\b",
    re.I)


def english_ratio(text):
    """（英字の割合, 数えた文字数）。コード・URL・パスは数えない。"""
    t = FENCE.sub(" ", text)
    t = INLINE.sub(" ", t)
    t = LINK_TARGET.sub("]", t)
    t = URL.sub(" ", t)
    t = PATH.sub(" ", t)
    en, jp = len(EN.findall(t)), len(JP.findall(t))
    total = en + jp
    return (en / total if total else 0.0), total


def texts(transcript):
    """会話記録の、Claude の文のエントリ（uuid, 文）を順に。"""
    out = []
    try:
        with open(transcript, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "assistant":
                    continue
                content = d.get("message", {}).get("content")
                if not isinstance(content, list):
                    continue
                body = "\n".join(c.get("text", "") for c in content if c.get("type") == "text")
                if body.strip():
                    out.append((d.get("uuid", ""), body))
    except OSError:
        pass
    return out


def load_state(session):
    """（調べ終えたエントリの uuid, 前回の発火時刻）。初めてのセッションでは (None, 0.0)。
    旧形式（uuid のリストだけ）の状態ファイルも読める。"""
    try:
        with open(os.path.join(STATE_DIR, f"{session}.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, 0.0
    if isinstance(data, list):
        return set(data), 0.0
    if isinstance(data, dict):
        try:
            last = float(data.get("last_fire", 0) or 0)
        except (TypeError, ValueError):
            last = 0.0
        return set(data.get("seen", [])), last
    return None, 0.0


def save_state(session, uuids, last_fire):
    """調べ終えたエントリの uuid を、会話記録の順で最新の 2000 件まで、前回の発火時刻とともに保存する。"""
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, f"{session}.json"), "w", encoding="utf-8") as f:
        json.dump({"seen": uuids[-2000:], "last_fire": last_fire}, f)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "tool"
    for stream in (sys.stdin, sys.stdout):  # 標準入出力が cp932 の環境でも、フックの JSON を UTF-8 で読み書きする
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    data = json.load(sys.stdin)
    transcript = data.get("transcript_path", "")
    session = re.sub(r"[^\w-]", "_", data.get("session_id", "unknown"))
    seen, last_fire = load_state(session)
    entries = texts(transcript)
    if seen is None:  # 途中から入れたセッション: それまでの文は調べず、最新の 1 つだけを見る
        seen = {u for u, _ in entries[:-1]}
    new = [(u, t) for u, t in entries if u not in seen]
    bad = []
    for u, t in new:
        ratio, total = english_ratio(t)
        if total >= MIN_CHARS and ratio > THRESHOLD and not REFUSAL.search(t):  # 拒否・安全上の応答らしい文は対象外
            bad.append((ratio, t))
        seen.add(u)
    now = time.time()
    emit = False
    if bad and now - last_fire >= COOLDOWN_SEC:  # 前回の発火から 30 秒以内は何も返さない
        if not (mode == "stop" and data.get("stop_hook_active")):  # 差し戻しは 1 回だけ（止まらなくなるのを防ぐ）
            emit = True
            last_fire = now
    save_state(session, [u for u, _ in entries if u in seen], last_fire)  # 会話記録の順で保存する（uuid の文字列順にしない）
    if not emit:
        return
    ratio, sample = bad[-1]
    head = sample.strip().splitlines()[0][:80]
    msg = (f"ユーザーに見える文が英語になっています（英字の割合 {ratio:.0%}。冒頭: 「{head}」）。"
           "このプロジェクトでは、途中経過も含めてすべて日本語（です・ます調）で書く決まりです。")
    if mode == "stop":
        print(json.dumps({"decision": "block",
                          "reason": msg + "直前の応答を、内容を変えずに日本語で書き直してください。"},
                         ensure_ascii=False))
    else:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                 "additionalContext": msg + "次の文から日本語で書いてください。"}},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()
