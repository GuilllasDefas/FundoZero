import argparse
from PIL import Image
from app.utils import remove_with_rembg, remove_with_grabcut


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--input', '-i', required=True)
    p.add_argument('--output', '-o', required=True)
    p.add_argument('--method', choices=['rembg', 'grabcut'], default='rembg')
    p.add_argument('--alpha-matting', action='store_true')
    args = p.parse_args()

    img = Image.open(args.input).convert('RGBA')
    if args.method == 'rembg':
        out = remove_with_rembg(img, alpha_matting=args.alpha_matting)
    else:
        out = remove_with_grabcut(img)
    out.save(args.output)

if __name__ == '__main__':
    main()