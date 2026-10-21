"""
Blank out lua strings and comments before anything counts keywords in them.

Every lua measurement in this package was line based, and a line based parser reads the inside of
a long string as code. This is real:

    local sql = [[
        function end end end do do
    ]]

That block made the function parser report five functions in a three function file, because the
words inside the string were counted as block keywords.

Long brackets come in levels -- [[ ]], [=[ ]=], [==[ ]==] -- and a closer only counts at its own
level, so [==[ ]] ]==] is one string containing a ]]. Short strings can escape their own quote.
Comments come in both forms, --[[ ]] and -- to end of line.

Everything is replaced with spaces rather than removed, so every line number and column stays
where it was. A caller can count keywords on the result and trust the positions.
"""

import re

LONG_OPEN = re.compile(r"(--)?\[(=*)\[")


def strip(src):
    """Source with strings and comments blanked to spaces, newlines kept."""
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        ch = src[i]

        # long bracket, comment or string, at any level
        m = LONG_OPEN.match(src, i)
        if m:
            level = m.group(2)
            close = "]" + level + "]"
            end = src.find(close, m.end())
            end = n if end < 0 else end + len(close)
            for j in range(i, end):
                if out[j] != "\n":
                    out[j] = " "
            i = end
            continue

        # line comment
        if src.startswith("--", i):
            end = src.find("\n", i)
            end = n if end < 0 else end
            for j in range(i, end):
                out[j] = " "
            i = end
            continue

        # short string, honouring escapes, never crossing newlines
        if ch in "\"'":
            j = i + 1
            while j < n and src[j] != ch and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            end = min(j + 1, n)
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue

        i += 1
    return "".join(out)


def comment_spans(src):
    """(start, end, text) for every comment, long or line, in source order.

    The comment scanners need the opposite of strip: the comments themselves, with their positions,
    and without anything that merely looks like one inside a string."""
    out = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        m = LONG_OPEN.match(src, i)
        if m:
            level = m.group(2)
            close = "]" + level + "]"
            end = src.find(close, m.end())
            end = n if end < 0 else end + len(close)
            if m.group(1):
                out.append((i, end, src[m.end():end - len(close)]))
            i = end
            continue
        if src.startswith("--", i):
            end = src.find("\n", i)
            end = n if end < 0 else end
            out.append((i, end, src[i + 2:end]))
            i = end
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and src[j] != ch and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            i = min(j + 1, n)
            continue
        i += 1
    return out
