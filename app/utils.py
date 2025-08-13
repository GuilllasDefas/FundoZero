from io import BytesIO
import numpy as np
from PIL import Image, ImageFilter
import cv2
import sys
import hashlib
from functools import lru_cache
from typing import Optional, Tuple, Literal, Dict, Any

# rembg agora opcional novamente para não quebrar se ausente
try:  # pragma: no cover - ambiente pode não ter rembg
    from rembg import remove, new_session  # type: ignore
    HAS_REMBG = True
    try:
        _REMBG_SESSION = new_session()  # carrega modelo uma vez
    except Exception:
        _REMBG_SESSION = None
except Exception:  # pragma: no cover
    HAS_REMBG = False
    _REMBG_SESSION = None

try:
    import pymatting
    HAS_PY_MATTING = True
except Exception:
    HAS_PY_MATTING = False


def pil_to_bytes(img: Image.Image, fmt="PNG") -> bytes:
    b = BytesIO()
    img.save(b, fmt)
    return b.getvalue()


def bytes_to_pil(b: bytes) -> Image.Image:
    return Image.open(BytesIO(b)).convert("RGBA")


# --- Remover com rembg (usa U2Net por baixo quando instalado) ---
def remove_with_rembg(
    pil_img: Image.Image,
    *,
    alpha_matting: bool = False,
    alpha_matting_foreground_threshold: int = 240,
    alpha_matting_background_threshold: int = 10,
    alpha_matting_erode_size: int = 10,
    downscale_max_dim: Optional[int] = 1600,
) -> Image.Image:
    """Remove fundo via rembg com opção de downscale para velocidade.
    Se a imagem for muito grande, reduz para `downscale_max_dim` antes de segmentar e reescala a máscara.
    """
    if not HAS_REMBG:
        raise RuntimeError("rembg não está instalado no ambiente")
    orig = pil_img.convert("RGBA")
    w, h = orig.size
    scale = 1.0
    work = orig
    if downscale_max_dim and max(w, h) > downscale_max_dim:
        scale = downscale_max_dim / float(max(w, h))
        new_size = (int(w * scale), int(h * scale))
        work = orig.resize(new_size, Image.LANCZOS)

    inp = pil_to_bytes(work)
    out_bytes = remove(
        inp,
        session=_REMBG_SESSION,
        alpha_matting=alpha_matting,
        alpha_matting_foreground_threshold=int(alpha_matting_foreground_threshold),
        alpha_matting_background_threshold=int(alpha_matting_background_threshold),
        alpha_matting_erode_size=int(alpha_matting_erode_size),
    )
    out_small = bytes_to_pil(out_bytes)
    if scale != 1.0:
        # reescala alpha para o tamanho original
        arr_small = np.array(out_small)
        alpha_small = arr_small[:, :, 3]
        alpha_full = cv2.resize(alpha_small, (w, h), interpolation=cv2.INTER_LINEAR)
        base = np.array(orig)
        base[:, :, 3] = alpha_full
        return Image.fromarray(base)
    return out_small


# --- Remover com GrabCut (fallback) ---
def remove_with_grabcut(pil_img: Image.Image, iter_count=5, rect_margin=10) -> Image.Image:
    img = np.array(pil_img.convert('RGB'))
    h, w = img.shape[:2]
    rect = (rect_margin, rect_margin, w - rect_margin * 2, h - rect_margin * 2)
    mask = np.zeros((h, w), np.uint8)
    bgdModel = np.zeros((1, 65), np.float64)
    fgdModel = np.zeros((1, 65), np.float64)
    cv2.grabCut(img, mask, rect, bgdModel, fgdModel, iter_count, cv2.GC_INIT_WITH_RECT)
    # mask: 0,2 = background; 1,3 = foreground
    mask2 = np.where((mask == 2) | (mask == 0), 0, 255).astype('uint8')
    # smooth mask a bit
    mask2 = cv2.medianBlur(mask2, 5)
    # compose
    rgba = cv2.cvtColor(img, cv2.COLOR_RGB2RGBA)
    rgba[:, :, 3] = mask2
    return Image.fromarray(rgba)


# --- Morphology operations on alpha channel ---

def refine_alpha(pil_img: Image.Image, erode=0, dilate=0, blur_radius=0):
    img = pil_img.convert('RGBA')
    arr = np.array(img)
    alpha = arr[:, :, 3]
    # erode
    if erode > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (erode, erode))
        alpha = cv2.erode(alpha, kernel, iterations=1)
    if dilate > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilate, dilate))
        alpha = cv2.dilate(alpha, kernel, iterations=1)
    if blur_radius > 0:
        # gaussian blur requires float
        alpha = cv2.GaussianBlur(alpha.astype(np.uint8), (0, 0), blur_radius)
    arr[:, :, 3] = alpha
    return Image.fromarray(arr)


# --- Feather (borda suave) via alpha blur ---

def feather_alpha(pil_img: Image.Image, radius=5):
    if radius <= 0:
        return pil_img
    img = pil_img.convert('RGBA')
    alpha = img.split()[-1]
    alpha = alpha.filter(ImageFilter.GaussianBlur(radius=radius))
    rgb = Image.new('RGBA', img.size)
    rgb.paste(img.convert('RGBA'))
    rgb.putalpha(alpha)
    return rgb


# --- Replace background (color or image) ---

def replace_background(foreground_pil: Image.Image, bg_color=None, bg_image: Image.Image=None) -> Image.Image:
    fg = foreground_pil.convert('RGBA')
    w, h = fg.size
    if bg_image is not None:
        bg = bg_image.convert('RGBA').resize((w, h))
    else:
        bg = Image.new('RGBA', (w, h), bg_color if bg_color is not None else (255, 255, 255, 255))
    # composite
    return Image.alpha_composite(bg, fg)


# --- Optional: closed-form matting refinement via pymatting if disponível ---

def refine_with_pymatting(rgb_pil: Image.Image, trimap: np.ndarray) -> Image.Image:
    if not HAS_PY_MATTING:
        raise RuntimeError('pymatting não está instalado')
    rgb = np.array(rgb_pil.convert('RGB')).astype(np.float64) / 255.0
    fg, alpha = pymatting.color_trimap(alpha_trimap=trimap, image=rgb)
    # fg in [0,1], alpha in [0,1]
    out = (fg * 255).astype(np.uint8)
    a = (alpha * 255).astype(np.uint8)
    rgba = np.dstack([out, a])
    return Image.fromarray(rgba)


# --- Otimizações & Pipeline unificado ---

def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


SegMethod = Literal['rembg', 'grabcut']


@lru_cache(maxsize=128)
def _cached_segment(mask_key: Tuple[str, SegMethod, int, int, int, bool, int, int, int]) -> Image.Image:
    """Executa somente a segmentação pesada e retorna RGBA (foreground com alpha).
    mask_key contém: (img_hash, method, rect_margin, iter_count, dummy, alpha_matting, fore_th, back_th, erode_size)
    dummy (3º índice) é placeholder para compat LRU (não usado para rembg, mas mantém assinatura estável).
    """
    (img_hash, method, rect_margin, iter_count, _dummy, alpha_matting, fore_th, back_th, erode_size) = mask_key
    # impossível reconstruir a imagem só do hash => esta função deve ser usada internamente passando já a imagem via closure.
    raise RuntimeError('Esta função não deve ser chamada diretamente; use process_image.')


def process_image(
    pil_img: Image.Image,
    *,
    method: SegMethod = 'rembg',
    alpha_matting: bool = True,
    alpha_matting_foreground_threshold: int = 240,
    alpha_matting_background_threshold: int = 10,
    alpha_matting_erode_size: int = 10,
    grabcut_iter: int = 5,
    grabcut_rect_margin: int = 15,
    max_dim: Optional[int] = 2000,
    erode: int = 0,
    dilate: int = 0,
    feather: int = 3,
    smooth_edges: bool = True,
    bg_mode: Literal['transparent', 'color', 'image'] = 'transparent',
    bg_color: Optional[Tuple[int, int, int]] = None,
    bg_image: Optional[Image.Image] = None,
) -> Image.Image:
    """Pipeline completo otimizado.
    - Redimensiona temporariamente se max_dim definido.
    - Cacheia somente a segmentação (mask) usando hash da imagem original + parâmetros relevantes.
    - Aplica refinamentos e composição de fundo.
    """
    orig = pil_img.convert('RGBA')
    w, h = orig.size
    scale = 1.0
    work = orig
    if max_dim and max(w, h) > max_dim:
        scale = max_dim / float(max(w, h))
        new_size = (int(w * scale), int(h * scale))
        work = orig.resize(new_size, Image.LANCZOS)

    img_bytes = pil_to_bytes(work)
    img_hash = _hash_bytes(img_bytes)

    # Executa segmentação com cache manual porque precisamos da imagem.
    cache_key = (img_hash, method, grabcut_rect_margin, grabcut_iter, 0,
                 alpha_matting, alpha_matting_foreground_threshold,
                 alpha_matting_background_threshold, alpha_matting_erode_size)

    segmented: Optional[Image.Image] = None
    if cache_key in _segment_cache:
        segmented = _segment_cache[cache_key]
    else:
        if method == 'rembg' and HAS_REMBG:
            segmented = remove_with_rembg(work,
                                          alpha_matting=alpha_matting,
                                          alpha_matting_foreground_threshold=alpha_matting_foreground_threshold,
                                          alpha_matting_background_threshold=alpha_matting_background_threshold,
                                          alpha_matting_erode_size=alpha_matting_erode_size)
        else:
            segmented = remove_with_grabcut(work, iter_count=grabcut_iter, rect_margin=grabcut_rect_margin)
        _segment_cache[cache_key] = segmented

    rgba = segmented

    # Se escalou, precisa retornar ao tamanho original — escala máscara/alpha e aplica sobre original.
    if scale != 1.0:
        arr_small = np.array(rgba)
        alpha_small = arr_small[:, :, 3]
        alpha_full = cv2.resize(alpha_small, (w, h), interpolation=cv2.INTER_LINEAR)
        base_rgb = np.array(orig)
        base_rgb[:, :, 3] = alpha_full
        rgba = Image.fromarray(base_rgb)

    # Refinamentos
    rgba = refine_alpha(rgba, erode=erode, dilate=dilate, blur_radius=0)
    rgba = feather_alpha(rgba, radius=feather)

    if smooth_edges:
        arr = np.array(rgba)
        alpha_ch = arr[:, :, 3]
        rgb = arr[:, :, :3]
        # bilateral em baixa resolução se grande (otimização)
        if max(arr.shape[0], arr.shape[1]) > 1200:
            small = cv2.bilateralFilter(cv2.resize(rgb, (0, 0), fx=0.5, fy=0.5), d=7, sigmaColor=60, sigmaSpace=60)
            rgb = cv2.resize(small, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)
        else:
            rgb = cv2.bilateralFilter(rgb, d=7, sigmaColor=60, sigmaSpace=60)
        arr[:, :, :3] = rgb
        rgba = Image.fromarray(arr)

    # Composição
    if bg_mode == 'transparent':
        final = rgba
    elif bg_mode == 'color':
        color_rgba = (bg_color[0], bg_color[1], bg_color[2], 255) if bg_color else (255, 255, 255, 255)
        final = replace_background(rgba, bg_color=color_rgba)
    else:
        final = replace_background(rgba, bg_image=bg_image) if bg_image else rgba

    return final


# cache simples baseado em dict para imagens processadas (segmentação)
_segment_cache: Dict[Tuple[str, SegMethod, int, int, int, bool, int, int, int], Image.Image] = {}

def cache_info() -> Dict[str, Any]:
    return {
        'segment_cache_size': len(_segment_cache)
    }