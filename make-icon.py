
#!/usr/bin/env python3
"""Convert artwork into a multi-resolution Windows ICO."""

import argparse
from pathlib import Path

from PIL import Image


ICON_SIZES = [16, 24, 32, 48, 64, 128, 256]


def make_icon(source: Path, output: Path) -> None:
    with Image.open(source) as image:
        # ICO artwork should be square; crop from the center if needed.
        image = image.convert("RGBA")
        width, height = image.size

        if width != height:
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            image = image.crop(
                (left, top, left + side, top + side)
            )

        # Pillow generates the requested resolutions in the ICO.
        image.save(
            output,
            format="ICO",
            sizes=[(size, size) for size in ICON_SIZES],
        )

    print(f"Created: {output.resolve()}")
    print(f"Included sizes: {', '.join(map(str, ICON_SIZES))}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a Windows ICO from image artwork."
    )
    parser.add_argument("source", type=Path, help="Source PNG or image")
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path("war-dogs.ico"),
        help="Output ICO path (default: war-dogs.ico)",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        parser.error(f"Image not found: {args.source}")

    make_icon(args.source, args.output)


if __name__ == "__main__":
    main()
