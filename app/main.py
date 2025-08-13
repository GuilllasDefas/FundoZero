import streamlit as st
from PIL import Image
from io import BytesIO
import numpy as np

# Some utils need cv2; import and expose a friendly flag
try:
    import cv2
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

from utils import (
    remove_with_rembg,
    remove_with_grabcut,
    refine_alpha,
    feather_alpha,
    replace_background,
    HAS_REMBG
)

st.set_page_config(page_title='Remover Fundo', layout='wide')

st.title('Remover Fundo — App')
st.markdown('App local para remover fundos com ajustes finos — carregue uma imagem e teste as opções.')

# Sidebar: options
with st.sidebar:
    st.header('Opções gerais')
    method = st.selectbox('Método', options=['rembg (se disponível)', 'grabcut (OpenCV)'])
    if method.startswith('rembg') and not HAS_REMBG:
        st.warning('rembg não está instalado — instale para usar U^2-Net. O app usará GrabCut como fallback.')

    st.subheader('Ajustes de máscara')
    alpha_matting = st.checkbox('Alpha matting (se usar rembg)', value=True)
    fore_th = st.slider('Foreground threshold (alpha matting)', 0, 255, 240)
    back_th = st.slider('Background threshold (alpha matting)', 0, 255, 10)
    erode_size = st.slider('Alpha matting erode size', 0, 30, 10)

    st.subheader('Refinamento')
    erode = st.slider('Erode (pixels)', 0, 50, 0)
    dilate = st.slider('Dilate (pixels)', 0, 50, 0)
    feather = st.slider('Feather radius', 0, 50, 3)
    smooth_edges = st.checkbox('Aplicar suavização (bilateral) nas bordas', value=True)

    st.subheader('Substituir fundo')
    bg_mode = st.selectbox('Fundo', options=['transparent', 'color', 'image'])
    bg_color = None
    bg_image = None
    if bg_mode == 'color':
        bg_color = st.color_picker('Escolha a cor do fundo', '#FFFFFF')
    elif bg_mode == 'image':
        bg_image = st.file_uploader('Imagem de fundo (será redimensionada)', type=['png', 'jpg', 'jpeg'])

    st.markdown('---')
    st.write('Dica: aumente `feather` e `erode/dilate` para bordas mais suaves mas perdendo detalhes.')


uploaded = st.file_uploader('Carregue uma imagem', type=['png', 'jpg', 'jpeg'])
if uploaded:
    input_img = Image.open(uploaded).convert('RGBA')
    col1, col2 = st.columns(2)
    with col1:
        st.subheader('Original')
        st.image(input_img, use_column_width=True)

    # Process with visual feedback
    try:
        with st.spinner('Processando imagem...'):
            # choose method
            st.write(f'Usando método: {method}')
            if method.startswith('rembg') and HAS_REMBG:
                out = remove_with_rembg(input_img,
                                        alpha_matting=alpha_matting,
                                        alpha_matting_foreground_threshold=fore_th,
                                        alpha_matting_background_threshold=back_th,
                                        alpha_matting_erode_size=erode_size)
            else:
                out = remove_with_grabcut(input_img, iter_count=5, rect_margin=15)

            # refine
            out = refine_alpha(out, erode=erode, dilate=dilate, blur_radius=0)
            out = feather_alpha(out, radius=feather)

            # optional: smooth edges (apply bilateral on RGB)
            if smooth_edges and HAS_CV2:
                arr = np.array(out)
                rgb = arr[:, :, :3]
                # bilateralFilter expects uint8
                rgb = cv2.bilateralFilter(rgb.astype('uint8'), d=9, sigmaColor=75, sigmaSpace=75)
                arr[:, :, :3] = rgb
                out = Image.fromarray(arr.astype('uint8'), 'RGBA')

            # replace background
            if bg_mode == 'transparent':
                final = out
            elif bg_mode == 'color':
                # convert hex color to rgba
                hexcol = bg_color.lstrip('#')
                bg_rgba = tuple(int(hexcol[i:i+2], 16) for i in (0, 2, 4)) + (255,)
                final = replace_background(out, bg_color=bg_rgba)
            else:
                if bg_image is not None:
                    bg_img = Image.open(bg_image)
                    final = replace_background(out, bg_image=bg_img)
                else:
                    final = out

        with col2:
            st.subheader('Resultado')
            st.image(final, use_column_width=True)

        # Download button
        buf = BytesIO()
        final.convert('RGBA').save(buf, format='PNG')
        byte_im = buf.getvalue()

        st.download_button('Baixar PNG (com transparência)', data=byte_im, file_name='result.png', mime='image/png')
        st.success('Processamento concluído!')

    except Exception as e:
        # show full error to help debugging
        import traceback
        tb = traceback.format_exc()
        st.error(f'Erro no processamento: {e}')
        st.text(tb)

else:
    st.info('Carregue uma imagem para começar.')