from PIL import Image, ImageOps
import os

TARGET_SIZE = (2500, 1686)

files = [
    ("richmenu_register.png", "richmenu_register_resized.jpg"),
    ("richmenu_main.png", "richmenu_main_resized.jpg"),
]

for input_file, output_file in files:
    img = Image.open(input_file).convert("RGB")

    img = ImageOps.fit(
        img,
        TARGET_SIZE,
        method=Image.Resampling.LANCZOS,
        centering=(0.5, 0.5)
    )

    # ลดคุณภาพเพื่อให้ไฟล์ต่ำกว่า 1 MB
    quality = 85

    while quality >= 40:
        img.save(output_file, "JPEG", quality=quality, optimize=True)
        size_mb = os.path.getsize(output_file) / (1024 * 1024)

        print(output_file, "quality", quality, "size", round(size_mb, 2), "MB")

        if size_mb <= 0.95:
            break

        quality -= 5

    if os.path.getsize(output_file) / (1024 * 1024) > 1:
        print("WARNING: file still larger than 1 MB:", output_file)

print("done")