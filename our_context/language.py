import re


def assert_plain_japanese(text):
    """本文を簡潔な肯定文へ統一する。引用とコードは原文を保つ。"""
    prose = re.sub(r"```[\s\S]*?```", "", text)
    prose = "\n".join(
        line for line in prose.splitlines() if not line.lstrip().startswith(">")
    )
    prose = re.sub(r"「[^」]*」|『[^』]*』|`[^`]*`", "", prose)
    if re.search(r"では(?:ない|ありません)|わけでは", prose):
        raise ValueError("本文を簡潔な肯定文で整理して再実行してください。")
