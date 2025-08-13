from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from PIL import Image
from io import BytesIO
from typing import Optional

from .utils import (
    process_image,
    HAS_REMBG,
    cache_info
)

app = FastAPI(title="FundoZero API", version="1.0.0")

@app.get("/health")
def health():
    return {"status": "ok", "rembg": HAS_REMBG, **cache_info()}

@app.post("/remove-background")
async def remove_background(
    file: UploadFile = File(...),
    method: str = Form("rembg"),
    alpha_matting: bool = Form(True),
    fore_th: int = Form(240),
    back_th: int = Form(10),
    erode_size: int = Form(10),
    grabcut_iter: int = Form(5),
    grabcut_margin: int = Form(15),
    max_dim: int = Form(2000),
    erode: int = Form(0),
    dilate: int = Form(0),
    feather: int = Form(3),
    smooth_edges: bool = Form(True),
    bg_mode: str = Form("transparent"),
    bg_color: Optional[str] = Form(None)
):
    try:
        raw = await file.read()
        img = Image.open(BytesIO(raw)).convert('RGBA')
    except Exception:
        raise HTTPException(status_code=400, detail="Imagem inválida")

    color_tuple = None
    if bg_color:
        hexcol = bg_color.lstrip('#')
        if len(hexcol) == 6:
            color_tuple = tuple(int(hexcol[i:i+2], 16) for i in (0,2,4))

    final = process_image(
        img,
        method='rembg' if method == 'rembg' else 'grabcut',
        alpha_matting=alpha_matting,
        alpha_matting_foreground_threshold=fore_th,
        alpha_matting_background_threshold=back_th,
        alpha_matting_erode_size=erode_size,
        grabcut_iter=grabcut_iter,
        grabcut_rect_margin=grabcut_margin,
        max_dim=max_dim,
        erode=erode,
        dilate=dilate,
        feather=feather,
        smooth_edges=smooth_edges,
        bg_mode=bg_mode if bg_mode in ("transparent","color","image") else 'transparent',
        bg_color=color_tuple
    )

    buf = BytesIO()
    final.save(buf, format='PNG')
    return Response(content=buf.getvalue(), media_type='image/png')
