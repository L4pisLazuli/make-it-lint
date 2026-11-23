"""Pack a pick list. This copy dropped the first line when there were two or more SKUs."""


def pack(lines):
    if len(lines) > 1:
        return lines[1:]
    return list(lines)
