"""
render_story.py — Story card compositor for KenauShorts.

Renders multi-line text stories, quotes, and image galleries over a looping background/ambient video.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from core.render import (
    Account,
    DEFAULT_LAYOUT,
    UI_CANDIDATES,
    UI_REGULAR_CANDIDATES,
    composite,
    draw_verified_badge,
    load_font,
    make_avatar,
    rounded_mask,
    wrap_headline,
)

LOG = logging.getLogger("kenaushorts.render_story")

DEFAULT_STORY_LAYOUT: dict[str, Any] = {
    "canvas_width": 1080,
    "canvas_height": 1920,
    "background": "#000000",
    "card_width": 936,
    "card_padding": 48,
    "card_radius": 36,
    "card_background": "#16181C",
    "card_opacity": 0.90,
    "background_dim": 0.35,
    "background_zoom": 1.0,
    "background_anchor_x": 0.5,
    "background_anchor_y": 0.5,
    "vertical_bias": 0.42,
    "avatar_size": 84,
    "avatar_trim": True,
    "avatar_zoom": 1.0,
    "name_size": 40,
    "handle_size": 32,
    "name_color": "#FFFFFF",
    "handle_color": "#8B98A5",
    "verified_color": "#1D9BF0",
    "verified_size": 36,
    "story_size": 42,
    "story_color": "#FFFFFF",
    "commentary_size": 36,
    "commentary_color": "#D0D5DD",
    "image_gap": 16,
    "image_radius": 24,
    "image_max_height": 420,
    "story_to_images": 28,
    "images_to_commentary": 28,
}

def fit_images(paths: list[Path], max_w: int, max_h: int, gap: int) -> list[Image.Image]:
    """
    Load images and size them to a single row that fits max_w x max_h.

    Side-by-side comparison shots are the point of this format, so the row is
    sized to a common height and the whole row scaled down if it overflows —
    rather than cropping, which would defeat a before/after pair.
    """
    loaded: list[Image.Image] = []
    for p in paths:
        try:
            loaded.append(Image.open(p).convert("RGB"))
        except (OSError, ValueError) as exc:
            LOG.warning("could not open image %s: %s", p, exc)
    if not loaded:
        return []

    target_h = min(max_h, max(im.height for im in loaded))
    scaled = [im.resize((max(1, int(im.width * target_h / im.height)), target_h),
                        Image.LANCZOS) for im in loaded]

    total_w = sum(im.width for im in scaled) + gap * (len(scaled) - 1)
    if total_w > max_w:
        factor = (max_w - gap * (len(scaled) - 1)) / sum(im.width for im in scaled)
        scaled = [im.resize((max(1, int(im.width * factor)),
                             max(1, int(im.height * factor))), Image.LANCZOS)
                  for im in scaled]
    return scaled

def paste_rounded(canvas: Image.Image, img: Image.Image, xy: tuple[int, int], radius: int) -> None:
    mask = rounded_mask(img.width, img.height, radius)
    canvas.paste(img, xy, mask)

def build_story_overlay(
    headline: str,
    commentary: str,
    images: list[Path],
    account: Account,
    layout: dict[str, Any],
    out_path: Path,
) -> Path:
    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    card_w = int(layout["card_width"])
    pad = int(layout["card_padding"])
    radius = int(layout["card_radius"])

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    # Scrim
    dim = float(layout.get("background_dim", 0.35))
    if dim > 0:
        alpha = int(255 * min(max(dim, 0.0), 1.0))
        scrim = Image.new("RGBA", (W, H), (0, 0, 0, alpha))
        canvas.alpha_composite(scrim)

    name_font = load_font(UI_CANDIDATES, int(layout["name_size"]), role="story_name")
    handle_font = load_font(UI_REGULAR_CANDIDATES, int(layout["handle_size"]), role="story_handle")
    story_font = load_font(UI_CANDIDATES, int(layout["story_size"]), role="story_text")
    comment_font = load_font(UI_REGULAR_CANDIDATES, int(layout["commentary_size"]), role="comment_text")

    inner_w = card_w - pad * 2
    story_line_h = int(layout["story_size"] * 1.35)
    story_lines = wrap_headline(headline, story_font, inner_w, draw)
    story_h = len(story_lines) * story_line_h

    comment_line_h = int(layout["commentary_size"] * 1.3)
    comment_lines = wrap_headline(commentary, comment_font, inner_w - 32, draw) if commentary else []
    comment_h = len(comment_lines) * comment_line_h + 24 if comment_lines else 0

    header_h = max(int(layout["avatar_size"]), int(layout["name_size"]) + int(layout["handle_size"]) + 8)

    image_gap = int(layout.get("image_gap", 16))
    image_radius = int(layout.get("image_radius", 24))
    image_max_h = int(layout.get("image_max_height", 420))
    story_to_images = int(layout.get("story_to_images", 28))
    images_to_commentary = int(layout.get("images_to_commentary", 28))

    fitted_images = fit_images(images, inner_w, image_max_h, image_gap) if images else []
    img_h = max((im.height for im in fitted_images), default=0)

    def card_height(image_row_h: int) -> int:
        h = pad + header_h + 32 + story_h
        if image_row_h:
            h += story_to_images + image_row_h
        if comment_h:
            h += images_to_commentary + comment_h
        return h + pad

    # Images are the only elastic element — text must stay legible, so a card
    # that would overflow the canvas shrinks its image row instead.
    max_card_h = H - 80
    guard = 0
    while fitted_images and card_height(img_h) > max_card_h and guard < 40:
        fitted_images = [im.resize((max(1, int(im.width * 0.94)), max(1, int(im.height * 0.94))), Image.LANCZOS)
                         for im in fitted_images]
        img_h = max(im.height for im in fitted_images)
        guard += 1

    card_h = card_height(img_h)
    bias = float(layout.get("vertical_bias", 0.42))
    card_x = (W - card_w) // 2
    card_y = max(0, int((H - card_h) * bias))

    # Draw card background
    card_layer = Image.new("RGBA", (card_w, card_h), (0, 0, 0, 0))
    opacity = int(255 * float(layout.get("card_opacity", 0.90)))
    card_rgb = Image.new("RGB", (1, 1), layout["card_background"]).getpixel((0, 0))
    bg_fill = card_rgb + (opacity,)
    ImageDraw.Draw(card_layer).rounded_rectangle([0, 0, card_w - 1, card_h - 1], radius=radius, fill=bg_fill)
    canvas.alpha_composite(card_layer, (card_x, card_y))

    # Draw Header inside card
    avatar = make_avatar(account.avatar, int(layout["avatar_size"]), account.name,
                          trim=bool(layout.get("avatar_trim", True)),
                          zoom=float(layout.get("avatar_zoom", 1.0)))
    canvas.alpha_composite(avatar, (card_x + pad, card_y + pad))

    tx = card_x + pad + int(layout["avatar_size"]) + 20
    ty = card_y + pad + 6
    draw.text((tx, ty), account.name, font=name_font, fill=layout["name_color"])
    if account.verified:
        bw = draw.textbbox((0, 0), account.name, font=name_font)[2]
        badge_sz = int(layout["verified_size"])
        draw_verified_badge(draw, tx + bw + 14 + badge_sz // 2, ty + badge_sz // 2 + 4, badge_sz, layout["verified_color"])
    draw.text((tx, ty + int(layout["name_size"]) + 6), account.handle, font=handle_font, fill=layout["handle_color"])

    # Draw Story
    sy = card_y + pad + header_h + 32
    for line in story_lines:
        draw.text((card_x + pad, sy), line, font=story_font, fill=layout["story_color"])
        sy += story_line_h

    # Draw image row, if any
    if fitted_images:
        sy += story_to_images
        row_w = sum(im.width for im in fitted_images) + image_gap * (len(fitted_images) - 1)
        ix = card_x + pad + (inner_w - row_w) // 2
        for im in fitted_images:
            paste_rounded(canvas, im, (ix, sy + (img_h - im.height) // 2), image_radius)
            ix += im.width + image_gap
        sy += img_h

    # Draw Commentary Quote Box if present
    if comment_lines:
        sy += images_to_commentary if fitted_images else 16
        draw.line([card_x + pad, sy, card_x + pad, sy + comment_h - 24], fill=layout["verified_color"], width=6)
        cy = sy + 6
        for line in comment_lines:
            draw.text((card_x + pad + 24, cy), line, font=comment_font, fill=layout["commentary_color"])
            cy += comment_line_h

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path)
    return out_path

def render_story(
    headline: str,
    commentary: str,
    images: list[Path],
    mascot: Path,
    out: Path,
    poster: Path | None = None,
    config: dict[str, Any] | None = None,
    duration: float = 25.0,
    music: Path | None = None,
) -> float:
    cfg = config or {}
    layout = {**DEFAULT_STORY_LAYOUT, **cfg.get("story_layout", {})}
    acct = Account(**{k: v for k, v in cfg.get("account", {}).items() if k in ("name", "handle", "avatar", "verified")})

    mascot = Path(mascot)
    if not mascot.exists():
        raise RuntimeError(
            f"story mascot loop not found: {mascot} — set story.mascot in "
            "config.json to a short looping background video."
        )

    out = Path(out)
    overlay = out.parent / f"{out.stem}_story_overlay.png"
    build_story_overlay(headline, commentary, images, acct, layout, overlay)

    W = int(layout["canvas_width"])
    H = int(layout["canvas_height"])
    # The mascot fills the entire canvas as the background; the overlay PNG
    # (transparent except for the floating card) is layered on top of it —
    # so the "video window" for composite() is simply the whole frame.
    box = (0, 0, W, H)

    dur = composite(
        video=Path(mascot),
        overlay=overlay,
        box=box,
        layout={**DEFAULT_LAYOUT, **layout},
        out=out,
        max_seconds=duration,
        poster=Path(poster) if poster else None,
        config=cfg,
        music=Path(music) if music else None,
        loop_video=True,  # the mascot is a short loop, not the content itself
    )
    return dur
