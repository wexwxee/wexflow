"""Генерирует прикольную иконку app.ico для лаунчера (ракета на градиенте)."""
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# --- фон: диагональный градиент синий (Salling) -> зелёный (7-Eleven) ---
top = (59, 130, 246)      # #3b82f6
bot = (22, 163, 74)       # #16a34a
grad = Image.new("RGBA", (S, S))
gd = ImageDraw.Draw(grad)
for y in range(S):
    t = y / (S - 1)
    r = int(top[0] + (bot[0] - top[0]) * t)
    g = int(top[1] + (bot[1] - top[1]) * t)
    b = int(top[2] + (bot[2] - top[2]) * t)
    gd.line([(0, y), (S, y)], fill=(r, g, b, 255))

# скруглённая маска
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=54, fill=255)
img.paste(grad, (0, 0), mask)
d = ImageDraw.Draw(img)

cx = S // 2
white = (255, 255, 255, 255)

# --- ракета ---
# корпус (капсула)
body_w, body_top, body_bot = 64, 70, 168
d.rounded_rectangle([cx - body_w // 2, body_top, cx + body_w // 2, body_bot],
                    radius=30, fill=white)
# нос
d.polygon([(cx - body_w // 2, body_top + 8), (cx, 34), (cx + body_w // 2, body_top + 8)], fill=white)
# иллюминатор
d.ellipse([cx - 18, 92, cx + 18, 128], fill=(59, 130, 246, 255))
d.ellipse([cx - 10, 100, cx + 10, 120], fill=(191, 219, 254, 255))
# крылья
d.polygon([(cx - body_w // 2, 130), (cx - body_w // 2 - 34, 178), (cx - body_w // 2, 168)], fill=white)
d.polygon([(cx + body_w // 2, 130), (cx + body_w // 2 + 34, 178), (cx + body_w // 2, 168)], fill=white)
# пламя
d.polygon([(cx - 22, body_bot - 4), (cx, 220), (cx + 22, body_bot - 4)], fill=(251, 191, 36, 255))
d.polygon([(cx - 12, body_bot - 2), (cx, 204), (cx + 12, body_bot - 2)], fill=(239, 68, 68, 255))

# --- зелёный бейдж с галочкой (авто-подача выполнена) ---
bx, by, br = 196, 196, 38
d.ellipse([bx - br, by - br, bx + br, by + br], fill=(22, 163, 74, 255), outline=white, width=5)
d.line([(bx - 16, by + 2), (bx - 4, by + 15), (bx + 18, by - 14)], fill=white, width=8, joint="curve")

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(r"C:\saling\app.ico", sizes=sizes)
print("app.ico создан")
