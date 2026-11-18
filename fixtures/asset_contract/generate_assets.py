"""Generate small deterministic local image assets for regression evidence.

The base fixture keeps three locked photos:
- asset-a.ppm: red product on a shared checkerboard template background.
- asset-b.ppm: asset-a rotated 90 degrees clockwise, then cropped 6px per edge.
- asset-c.ppm: same template background and silhouette, blue product instead.

Additional counter-examples (used by manifest-extended.json) are regenerated
with the same deterministic formulas:
- asset-a-copy.ppm: byte-identical copy of asset-a (an exact copy).
- asset-d-blue-copy.ppm: byte-identical copy of asset-c (same template family).
- asset-e-deepcrop.ppm: asset-a rotated 90 degrees clockwise and cropped 10px
  per edge, which exceeds the bounded max crop of 8px and must stay its own
  licensing unit even though every subject pixel is retained.
"""
from pathlib import Path

root = Path(__file__).resolve().parent


def photo(product):
    pixels = []
    for y in range(64):
        row = []
        for x in range(64):
            base = 130 + (12 if (x // 8 + y // 8) % 2 else -12)
            color = (base, base, base)
            if 16 <= x <= 47 and 23 <= y <= 42 and ((x - 30) ** 2 / 400 + (y - 32) ** 2 / 144 <= 1):
                color = (210, 35, 45) if product == 'red' else (35, 55, 210)
            if 18 <= x <= 42 and 42 <= y <= 47:
                color = (35, 35, 35)
            row.append(color)
        pixels.append(row)
    return pixels


def save(name, pixels):
    h = len(pixels)
    w = len(pixels[0])
    raw = b''.join(bytes(rgb) for row in pixels for rgb in row)
    (root / name).write_bytes(f'P6\n{w} {h}\n255\n'.encode() + raw)


def rotate90_cw(pixels):
    size = len(pixels)
    return [[pixels[size - 1 - x][y] for x in range(size)] for y in range(size)]


def crop_edges(pixels, cut):
    size = len(pixels) - 2 * cut
    return [row[cut:cut + size] for row in pixels[cut:cut + size]]


def main():
    a = photo('red')
    save('asset-a.ppm', a)
    rot = rotate90_cw(a)
    save('asset-b.ppm', crop_edges(rot, 6))
    save('asset-c.ppm', photo('blue'))

    # Extended counter-example fixture. Byte-identical copies keep the exact
    # SHA of their source; the deep crop stays inside the subject silhouette.
    save('asset-a-copy.ppm', a)
    save('asset-d-blue-copy.ppm', photo('blue'))
    save('asset-e-deepcrop.ppm', crop_edges(rot, 10))


if __name__ == '__main__':
    main()
