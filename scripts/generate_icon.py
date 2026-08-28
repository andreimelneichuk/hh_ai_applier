import os
import sys
import math
import subprocess
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# Обеспечиваем корректный вывод UTF-8 в консоль на любой ОС (включая Windows CI)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def create_app_icon():
    size = 1024
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Скругленный квадрат (Squircle) для фона
    margin = 40
    corner_radius = 210
    bg_box = [margin, margin, size - margin, size - margin]

    # Создаем градиентную маску для фона
    bg = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    
    # Рисуем темный фон с неоновым градиентом
    for y in range(margin, size - margin):
        ratio = (y - margin) / (size - 2 * margin)
        # От темно-синего/индиго к ультра-темному антрациту
        r = int(12 + ratio * 8)
        g = int(16 + ratio * 12)
        b = int(32 + ratio * 20)
        bg_draw.line([(margin, y), (size - margin, y)], fill=(r, g, b, 255))

    # Скругленная маска
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle(bg_box, radius=corner_radius, fill=255)

    # Неоновые сферы свечения внутри фона
    glow_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow_layer)
    
    # Верхнее циан/бирюзовое свечение
    glow_draw.ellipse([200, 100, 824, 724], fill=(14, 165, 233, 110))
    # Нижнее фиолетовое/индиго свечение
    glow_draw.ellipse([300, 400, 924, 1024], fill=(129, 140, 248, 120))
    
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(120))
    bg.paste(glow_layer, (0, 0), glow_layer)

    # Применяем маску скругления
    icon_base = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    icon_base.paste(bg, (0, 0), mask)
    
    # 2. Неоновая обводка (Border)
    border_draw = ImageDraw.Draw(icon_base)
    border_draw.rounded_rectangle(bg_box, radius=corner_radius, outline=(99, 102, 241, 160), width=6)

    # 3. Центральный символ: Стилизованный лист резюме с нейросетью и искрами
    center_x, center_y = size // 2, size // 2
    
    # Стеклянная плашка резюме
    doc_w, doc_h = 340, 440
    doc_x1, doc_y1 = center_x - doc_w // 2, center_y - doc_h // 2 + 20
    doc_x2, doc_y2 = doc_x1 + doc_w, doc_y1 + doc_h
    doc_radius = 32

    # Тень документа
    shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow)
    s_draw.rounded_rectangle([doc_x1, doc_y1 + 25, doc_x2, doc_y2 + 25], radius=doc_radius, fill=(0, 0, 0, 180))
    shadow = shadow.filter(ImageFilter.GaussianBlur(30))
    icon_base.alpha_composite(shadow)

    # Сам стеклянный документ
    doc_layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d_draw = ImageDraw.Draw(doc_layer)
    d_draw.rounded_rectangle([doc_x1, doc_y1, doc_x2, doc_y2], radius=doc_radius, fill=(255, 255, 255, 30))
    d_draw.rounded_rectangle([doc_x1, doc_y1, doc_x2, doc_y2], radius=doc_radius, outline=(255, 255, 255, 90), width=4)

    # Линии текста резюме
    line_y = doc_y1 + 60
    line_x1 = doc_x1 + 40
    
    # Заголовок резюме
    d_draw.rounded_rectangle([line_x1, line_y, line_x1 + 140, line_y + 20], radius=8, fill=(56, 189, 248, 220))
    
    # Строки контента
    line_y += 50
    d_draw.rounded_rectangle([line_x1, line_y, doc_x2 - 40, line_y + 14], radius=6, fill=(255, 255, 255, 120))
    line_y += 35
    d_draw.rounded_rectangle([line_x1, line_y, doc_x2 - 80, line_y + 14], radius=6, fill=(255, 255, 255, 100))
    line_y += 35
    d_draw.rounded_rectangle([line_x1, line_y, doc_x2 - 60, line_y + 14], radius=6, fill=(255, 255, 255, 80))
    
    # 4. Неоновый знак ИИ / Молния / Нейронный импульс поверх резюме
    # Рисуем стилизованную молнию / звезду ИИ
    badge_cx, badge_cy = doc_x2 - 20, doc_y1 + 40
    badge_r = 75
    
    # Свечение бейджа
    b_glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    bg_draw2 = ImageDraw.Draw(b_glow)
    bg_draw2.ellipse([badge_cx - badge_r - 40, badge_cy - badge_r - 40, badge_cx + badge_r + 40, badge_cy + badge_r + 40], fill=(236, 72, 153, 140))
    bg_draw2.ellipse([badge_cx - badge_r - 20, badge_cy - badge_r - 20, badge_cx + badge_r + 20, badge_cy + badge_r + 20], fill=(168, 85, 247, 180))
    b_glow = b_glow.filter(ImageFilter.GaussianBlur(35))
    doc_layer.alpha_composite(b_glow)

    # Круг бейджа
    d_draw.ellipse([badge_cx - badge_r, badge_cy - badge_r, badge_cx + badge_r, badge_cy + badge_r], fill=(30, 27, 75, 240), outline=(192, 132, 252, 255), width=5)

    # Рисуем символ молнии / искры внутри бейджа
    sparkle_pts = [
        (badge_cx, badge_cy - 42),
        (badge_cx + 10, badge_cy - 12),
        (badge_cx + 42, badge_cy),
        (badge_cx + 10, badge_cy + 12),
        (badge_cx, badge_cy + 42),
        (badge_cx - 10, badge_cy + 12),
        (badge_cx - 42, badge_cy),
        (badge_cx - 10, badge_cy - 12),
    ]
    d_draw.polygon(sparkle_pts, fill=(255, 255, 255, 255))
    
    # Маленькая искра
    small_sparkle = [
        (badge_cx - 24, badge_cy - 24),
        (badge_cx - 20, badge_cy - 16),
        (badge_cx - 12, badge_cy - 20),
        (badge_cx - 20, badge_cy - 24),
    ]

    # Нижняя плашка с надписью HH • AI
    tag_w, tag_h = 240, 60
    tag_x1, tag_y1 = center_x - tag_w // 2, doc_y2 - 85
    d_draw.rounded_rectangle([tag_x1, tag_y1, tag_x1 + tag_w, tag_y1 + tag_h], radius=16, fill=(15, 23, 42, 230), outline=(56, 189, 248, 200), width=3)
    
    # Добавляем стилизованный текст HH AI
    font_large = None
    for font_path in [
        "/System/Library/Fonts/SFNS.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/segoeui.ttf",
        "C:/Windows/Fonts/arial.ttf",
    ]:
        try:
            if os.path.exists(font_path):
                font_large = ImageFont.truetype(font_path, 36)
                break
        except Exception:
            continue
    if font_large is None:
        font_large = ImageFont.load_default()

    d_draw.text((center_x, tag_y1 + tag_h // 2), "HH • AI", fill=(255, 255, 255, 255), font=font_large, anchor="mm")

    icon_base.alpha_composite(doc_layer)

    # 5. Сохранение в директорию assets и static
    os.makedirs("assets", exist_ok=True)
    os.makedirs("static", exist_ok=True)

    png_path = "assets/icon.png"
    icon_base.save(png_path, "PNG")
    icon_base.save("static/icon.png", "PNG")
    print(f"[OK] PNG icon created: {png_path}")

    # 6. Сохранение .ico для Windows (мульти-разрешение)
    ico_sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    ico_images = [icon_base.resize(s, Image.Resampling.LANCZOS) for s in ico_sizes]
    ico_path = "assets/icon.ico"
    ico_images[0].save(ico_path, format="ICO", sizes=ico_sizes)
    ico_images[0].save("static/favicon.ico", format="ICO", sizes=ico_sizes)
    print(f"[OK] ICO icon created: {ico_path}")

    # 7. Генерация .icns для macOS через sips + iconutil
    try:
        iconset_dir = "assets/icon.iconset"
        os.makedirs(iconset_dir, exist_ok=True)
        
        sizes_map = {
            "icon_16x16.png": 16,
            "icon_16x16@2x.png": 32,
            "icon_32x32.png": 32,
            "icon_32x32@2x.png": 64,
            "icon_128x128.png": 128,
            "icon_128x128@2x.png": 256,
            "icon_256x256.png": 256,
            "icon_256x256@2x.png": 512,
            "icon_512x512.png": 512,
            "icon_512x512@2x.png": 1024,
        }
        for name, sz in sizes_map.items():
            resized = icon_base.resize((sz, sz), Image.Resampling.LANCZOS)
            resized.save(os.path.join(iconset_dir, name))

        subprocess.run(["iconutil", "-c", "icns", iconset_dir, "-o", "assets/icon.icns"], check=True)
        print("[OK] ICNS icon for macOS created: assets/icon.icns")
        # Удаляем временную папку iconset
        import shutil
        shutil.rmtree(iconset_dir, ignore_errors=True)
    except Exception as e:
        print(f"[INFO] iconutil skipped ({e}), using PNG")

if __name__ == "__main__":
    create_app_icon()
