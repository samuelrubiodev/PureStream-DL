"""Genera icon-192.png e icon-512.png para la PWA (Python stdlib, sin deps).

Fondo indigo (brand-600) a sangre + triangulo play blanco. Pensado para
iconos "maskable" (rellena todo el cuadrado). Se ejecuta en el build de Docker.
"""
import os
import struct
import zlib


def _in_tri(px, py, ax, ay, bx, by, cx, cy):
    d1 = (px - bx) * (ay - by) - (ax - bx) * (py - by)
    d2 = (px - cx) * (by - cy) - (bx - cx) * (py - cy)
    d3 = (px - ax) * (cy - ay) - (cx - ax) * (py - ay)
    neg = (d1 < 0) or (d2 < 0) or (d3 < 0)
    pos = (d1 > 0) or (d2 > 0) or (d3 > 0)
    return not (neg and pos)


def _png(path, size):
    bg = (79, 70, 229)   # #4f46e5 brand-600
    fg = (255, 255, 255)
    # Vertices del triangulo play (apunta a la derecha), centrado.
    ax, ay = size * 0.36, size * 0.30
    bx, by = size * 0.36, size * 0.70
    cx, cy = size * 0.72, size * 0.50

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter byte (None)
        for x in range(size):
            r, g, b = fg if _in_tri(x + 0.5, y + 0.5, ax, ay, bx, by, cx, cy) else bg
            raw += bytes((r, g, b))

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as f:
        f.write(sig)
        f.write(chunk(b"IHDR", ihdr))
        f.write(chunk(b"IDAT", idat))
        f.write(chunk(b"IEND", b""))


if __name__ == "__main__":
    d = os.path.dirname(os.path.abspath(__file__))
    for s in (192, 512):
        _png(os.path.join(d, f"icon-{s}.png"), s)
    print("icons generated: icon-192.png, icon-512.png")