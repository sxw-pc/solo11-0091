"""Generate small deterministic local image assets for regression evidence."""
from pathlib import Path
root=Path(__file__).resolve().parent

def photo(product):
    pixels=[]
    for y in range(64):
        row=[]
        for x in range(64):
            base=130 + (12 if (x//8+y//8)%2 else -12)
            color=(base,base,base)
            if 16<=x<=47 and 23<=y<=42 and ((x-30)**2/400+(y-32)**2/144<=1):
                color=(210,35,45) if product=='red' else (35,55,210)
            if 18<=x<=42 and 42<=y<=47: color=(35,35,35)
            row.append(color)
        pixels.append(row)
    return pixels

def save(name,pixels):
    h=len(pixels);w=len(pixels[0]);raw=b''.join(bytes(rgb) for row in pixels for rgb in row)
    (root/name).write_bytes(f'P6\n{w} {h}\n255\n'.encode()+raw)

a=photo('red')
save('asset-a.ppm',a)
rot=[[a[63-x][y] for x in range(64)] for y in range(64)]
save('asset-b.ppm',[row[6:58] for row in rot[6:58]])
save('asset-c.ppm',photo('blue'))
