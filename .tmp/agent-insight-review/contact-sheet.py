from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(r"D:\code\xiaodao\.tmp\agent-insight-review")
for label in ("1920x1080", "1366x768"):
    files = sorted(root.glob(f"{label}-slide-*.png"))
    thumb_w, thumb_h = 320, 180
    label_h = 26
    cols = 5
    rows = (len(files) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * thumb_w, rows * (thumb_h + label_h)), "#20242c")
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()
    for index, file in enumerate(files):
        image = Image.open(file).convert("RGB")
        image.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
        x = (index % cols) * thumb_w
        y = (index // cols) * (thumb_h + label_h)
        sheet.paste(image, (x, y))
        draw.rectangle((x, y + thumb_h, x + thumb_w, y + thumb_h + label_h), fill="#20242c")
        draw.text((x + 8, y + thumb_h + 6), f"{index + 1:02d} / 33", fill="white", font=font)
    sheet.save(root / f"contact-{label}.png")
