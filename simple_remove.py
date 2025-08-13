#!/usr/bin/env python
"""Script simples para remover fundo de uma ou várias imagens sem precisar de API ou GUI.

Uso básico:
  python simple_remove.py input.jpg
  -> gera input_no_bg.png ao lado.

Opções:
  python simple_remove.py input1.jpg input2.png -o saida_dir --bg-color "#FFFFFF" --method grabcut

Requisitos: pillow, numpy, opencv-python, (opcional) rembg, pymatting.
"""
from __future__ import annotations
import argparse
from pathlib import Path
from typing import Optional
from PIL import Image

from app.utils import (
    remove_with_rembg,
    remove_with_grabcut,
    refine_alpha,
    feather_alpha,
    replace_background,
    HAS_REMBG
)


def parse_args():
    p = argparse.ArgumentParser(description='Remover fundo de imagens de forma simples.')
    p.add_argument('inputs', nargs='+', help='Arquivos de imagem de entrada')
    p.add_argument('-o', '--output-dir', help='Diretório de saída (default = mesmo da imagem)')
    p.add_argument('--method', choices=['auto', 'rembg', 'grabcut'], default='auto', help='Método de segmentação')
    p.add_argument('--alpha-matting', action='store_true', help='Ativa alpha matting (rembg)')
    p.add_argument('--erode', type=int, default=0, help='Erode na máscara')
    p.add_argument('--dilate', type=int, default=0, help='Dilate na máscara')
    p.add_argument('--feather', type=int, default=3, help='Suavização de borda (raio)')
    p.add_argument('--bg-color', type=str, default=None, help='Cor de fundo ex: #FFFFFF (se omitido mantém transparente)')
    p.add_argument('--max-dim', type=int, default=0, help='Reduz maior dimensão antes de processar (0 = não)')
    return p.parse_args()


def choose_method(requested: str) -> str:
    if requested == 'auto':
        return 'rembg' if HAS_REMBG else 'grabcut'
    if requested == 'rembg' and not HAS_REMBG:
        print('[aviso] rembg indisponível, usando grabcut.')
        return 'grabcut'
    return requested


def hex_to_rgb_tuple(h: str) -> tuple[int, int, int]:
    h = h.lstrip('#')
    if len(h) != 6:
        raise ValueError('Cor inválida, use formato #RRGGBB')
    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))


def process_one(path: Path, out_dir: Optional[Path], args) -> Path:
    img = Image.open(path).convert('RGBA')

    # Optional downscale para acelerar
    if args.max_dim and args.max_dim > 0:
        w, h = img.size
        if max(w, h) > args.max_dim:
            scale = args.max_dim / float(max(w, h))
            new_size = (int(w * scale), int(h * scale))
            img = img.resize(new_size, Image.LANCZOS)

    method = choose_method(args.method)
    if method == 'rembg':
        out = remove_with_rembg(img, alpha_matting=args.alpha_matting)
    else:
        out = remove_with_grabcut(img)

    out = refine_alpha(out, erode=args.erode, dilate=args.dilate)
    out = feather_alpha(out, radius=args.feather)

    if args.bg_color:
        rgb = hex_to_rgb_tuple(args.bg_color)
        out = replace_background(out, bg_color=rgb + (255,))

    if out_dir is None:
        out_dir = path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    out_name = path.stem + '_no_bg.png'
    out_path = out_dir / out_name
    out.save(out_path, 'PNG')
    return out_path


def main():
    args = parse_args()
    out_dir = Path(args.output_dir) if args.output_dir else None
    successes = 0
    for p in args.inputs:
        try:
            out_path = process_one(Path(p), out_dir, args)
            print(f'[ok] {p} -> {out_path}')
            successes += 1
        except Exception as e:
            print(f'[erro] {p}: {e}')
    print(f'Concluído: {successes}/{len(args.inputs)} imagens processadas.')


if __name__ == '__main__':
    main()
