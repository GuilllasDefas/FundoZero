"""FundoZero GUI

Fluxo:
    Abrir imagem -> Processar -> (Ajustes / Ferramentas) -> Salvar

Modos:
    Auto / Qualidade / Rápido (rembg se disponível ou GrabCut)

Ferramentas de correção:
    - Pincel Recuperar / Remover
    - Varinha (Varinha+ adiciona, Varinha- remove) com tolerância
    - Undo (até 8 passos)

Outros:
    - Ajustes de suavização (feather), erode, dilate
    - Fundo transparente ou cor sólida
    - Zoom, Ajustar, duplo clique alterna modo
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple, List
from collections import deque
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser
from PIL import Image, ImageTk, ImageDraw, ImageFilter, ImageChops

try:
    from app.utils import (
        remove_with_rembg,
        remove_with_grabcut,
        refine_alpha,
        feather_alpha,
        replace_background,
        HAS_REMBG,
    )
except Exception:  # pragma: no cover
    from app.utils import (
        remove_with_rembg,
        remove_with_grabcut,
        refine_alpha,
        feather_alpha,
        replace_background,
        HAS_REMBG,
    )

APP_TITLE = 'FundoZero'


def _auto_defaults(img: Image.Image) -> Tuple[int,int,int]:
    m = max(img.size)
    if m > 2500: return 0,0,2
    if m < 600: return 0,0,5
    return 0,0,3


class FundoZeroGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry('1000x640')
        self.root.minsize(880,520)
        self.root.configure(bg='#202020')

        # Estado imagem
        self.path: Optional[Path] = None
        self.orig: Optional[Image.Image] = None
        self.orig_rgb: Optional[Image.Image] = None
        self.segmented: Optional[Image.Image] = None
        self.mask: Optional[Image.Image] = None
        self.result: Optional[Image.Image] = None
        self.preview_scaled: Optional[Image.Image] = None
        self.preview_mask: Optional[Image.Image] = None
        self.orig_lab: Optional[Image.Image] = None
        self.bg_color: Optional[Tuple[int,int,int]] = None
        self.processing = False

        # Variáveis UI
        self.mode_var = tk.StringVar(value='Auto')
        self.bg_mode_var = tk.StringVar(value='transparent')
        self.status_var = tk.StringVar(value='Pronto')
        self.zoom_var = tk.DoubleVar(value=100.0)
        self.fit_var = tk.BooleanVar(value=True)
        self.feather_var = tk.IntVar(value=3)
        self.erode_var = tk.IntVar(value=0)
        self.dilate_var = tk.IntVar(value=0)
        self.adv_open = tk.BooleanVar(value=False)
        self.alpha_matting_var = tk.BooleanVar(value=False)
        self.brush_mode_var = tk.StringVar(value='Nenhum')
        self.brush_size_var = tk.IntVar(value=30)
        self.wand_tol_var = tk.IntVar(value=18)
        self.overlay_var = tk.BooleanVar(value=False)
        self._undo_stack: List[Image.Image] = []

        # Debounce / geometria
        self._debounce_resize_id = None
        self._debounce_hq_id = None
        self._disp_origin = (0,0)
        self._disp_img_size = (0,0)
        self._painting = False
        self._panning = False
        self._pan_start = (0,0)
        self._pan_offset = [0,0]
        self._space_pan = False

        self._build()
        self.root.bind('<Configure>', self._on_resize)
        self._set_status('Abra uma imagem')
        self.root.bind('m', lambda _e: self._toggle_overlay())
        self.root.bind('<KeyPress-space>', self._on_space_press)
        self.root.bind('<KeyRelease-space>', self._on_space_release)

    # ---------- UI ----------
    def _build(self):
            # Barra superior
            top = ttk.Frame(self.root)
            top.pack(fill=tk.X, padx=8, pady=6)
            ttk.Button(top, text='Abrir', command=self._on_open).pack(side=tk.LEFT)
            ttk.Button(top, text='Processar', command=self._on_process).pack(side=tk.LEFT, padx=4)
            ttk.Button(top, text='Salvar', command=self._on_save).pack(side=tk.LEFT)
            ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
            ttk.Label(top, text='Modo:').pack(side=tk.LEFT)
            self.mode_combo = ttk.Combobox(top, values=['Auto','Qualidade','Rápido'], textvariable=self.mode_var, state='readonly', width=10)
            self.mode_combo.pack(side=tk.LEFT, padx=2)
            self.mode_combo.bind('<<ComboboxSelected>>', lambda _e: self._sync_mode())
            ttk.Label(top, text='Fundo:').pack(side=tk.LEFT, padx=(10,2))
            self.bg_combo = ttk.Combobox(top, values=['transparent','color'], textvariable=self.bg_mode_var, state='readonly', width=11)
            self.bg_combo.pack(side=tk.LEFT)
            ttk.Button(top, text='Cor...', command=self._on_pick_color).pack(side=tk.LEFT, padx=2)
            ttk.Checkbutton(top, text='Alpha', variable=self.alpha_matting_var).pack(side=tk.LEFT, padx=4)
            ttk.Checkbutton(top, text='Overlay', variable=self.overlay_var, command=lambda: self._render_preview()).pack(side=tk.LEFT, padx=2)
            ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
            ttk.Button(top, text='Ajustes ▸', command=self._toggle_adv).pack(side=tk.LEFT)
            ttk.Separator(top, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=6)
            ttk.Label(top, text='Ferramenta:').pack(side=tk.LEFT)
            self.brush_combo = ttk.Combobox(top, values=['Nenhum','Recuperar','Remover','Varinha+','Varinha-'], textvariable=self.brush_mode_var, state='readonly', width=11)
            self.brush_combo.pack(side=tk.LEFT, padx=2)
            self.brush_combo.bind('<<ComboboxSelected>>', lambda _e: self._on_brush_change())
            ttk.Scale(top, from_=5, to=150, variable=self.brush_size_var, orient='horizontal', length=120).pack(side=tk.LEFT, padx=4)
            ttk.Label(top, text='Tol:').pack(side=tk.LEFT, padx=(6,2))
            ttk.Scale(top, from_=0, to=60, variable=self.wand_tol_var, orient='horizontal', length=90).pack(side=tk.LEFT, padx=2)
            ttk.Button(top, text='Undo', command=self._undo_mask).pack(side=tk.LEFT, padx=4)
            ttk.Label(top, textvariable=self.status_var, foreground='#888').pack(side=tk.RIGHT)

            # Centro com canvas e barras de rolagem
            center = ttk.Frame(self.root)
            center.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0,4))
            canvas_wrap = ttk.Frame(center)
            canvas_wrap.pack(fill=tk.BOTH, expand=True)
            self.canvas = tk.Canvas(canvas_wrap, bg='#2a2a2a', highlightthickness=0)
            self.v_scroll = ttk.Scrollbar(canvas_wrap, orient='vertical', command=self._scroll_y)
            self.h_scroll = ttk.Scrollbar(center, orient='horizontal', command=self._scroll_x)
            self.canvas.grid(row=0, column=0, sticky='nsew')
            self.v_scroll.grid(row=0, column=1, sticky='ns')
            canvas_wrap.grid_columnconfigure(0, weight=1)
            canvas_wrap.grid_rowconfigure(0, weight=1)
            self.h_scroll.pack(fill=tk.X, side=tk.BOTTOM)
            self.canvas.bind('<MouseWheel>', self._on_wheel)
            self.canvas.bind('<Double-Button-1>', lambda _e: self._toggle_fit())
            self.canvas.bind('<ButtonPress-1>', self._on_canvas_press)
            self.canvas.bind('<B1-Motion>', self._on_canvas_drag)
            self.canvas.bind('<ButtonRelease-1>', self._on_canvas_release)
            self.canvas.bind('<ButtonPress-2>', self._on_pan_press)
            self.canvas.bind('<B2-Motion>', self._on_pan_drag)
            self.canvas.bind('<ButtonRelease-2>', self._on_pan_release)

            # Painel avançado (oculto inicialmente)
            self.adv_panel = ttk.Frame(self.root)
            r=0
            ttk.Label(self.adv_panel, text='Suavização').grid(row=r,column=0,sticky='w'); r+=1
            ttk.Scale(self.adv_panel, from_=0,to=12,variable=self.feather_var).grid(row=r,column=0,sticky='we'); r+=1
            ttk.Label(self.adv_panel, text='Contrair (erode)').grid(row=r,column=0,sticky='w'); r+=1
            ttk.Scale(self.adv_panel, from_=0,to=10,variable=self.erode_var).grid(row=r,column=0,sticky='we'); r+=1
            ttk.Label(self.adv_panel, text='Expandir (dilate)').grid(row=r,column=0,sticky='w'); r+=1
            ttk.Scale(self.adv_panel, from_=0,to=10,variable=self.dilate_var).grid(row=r,column=0,sticky='we'); r+=1
            self.adv_panel.grid_columnconfigure(0, weight=1)

            # Barra inferior
            bottom = ttk.Frame(self.root)
            bottom.pack(fill=tk.X, padx=8, pady=(0,6))
            ttk.Label(bottom, text='Zoom:').pack(side=tk.LEFT)
            self.zoom_slider = ttk.Scale(bottom, from_=10,to=400, variable=self.zoom_var, command=lambda _e: self._on_zoom())
            self.zoom_slider.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
            ttk.Button(bottom, text='100%', command=lambda: self._set_zoom(100)).pack(side=tk.LEFT, padx=2)
            ttk.Button(bottom, text='Ajustar', command=self._fit).pack(side=tk.LEFT, padx=2)
            self.progress = ttk.Progressbar(bottom, mode='indeterminate', length=100)
            self.progress.pack(side=tk.RIGHT, padx=4)

    # ---------- Ações ----------
    def _on_open(self):
        path = filedialog.askopenfilename(title='Imagem', filetypes=[('Imagens','*.png;*.jpg;*.jpeg;*.webp'),('Todos','*.*')])
        if not path: return
        try:
            self.path = Path(path)
            self.orig = Image.open(self.path).convert('RGBA')
            self.orig_rgb = self.orig.convert('RGB')
            try:
                self.orig_lab = self.orig_rgb.convert('LAB')
            except Exception:
                self.orig_lab = None
            e,d,f = _auto_defaults(self.orig)
            self.erode_var.set(e); self.dilate_var.set(d); self.feather_var.set(f)
            self.segmented = None; self.mask=None; self.result=None
            self._make_preview(); self._render_preview()
            self._set_status('Imagem carregada')
        except Exception as ex:  # pragma: no cover
            messagebox.showerror('Erro', f'Falha ao abrir: {ex}')

    def _on_process(self):
        if not self.orig or self.processing:
            return
        
        # Desativar botões durante o processamento
        self._toggle_buttons(state=tk.DISABLED)
        
        self.processing = True
        self.progress.start(12)
        self._set_status('Processando...')
        threading.Thread(target=self._do_process, daemon=True).start()

    def _do_process(self):
        try:
            img = self.orig
            method = self._resolve_method()
            alpha_flag = bool(self.alpha_matting_var.get()) and method=='rembg'
            if method=='rembg':
                seg = remove_with_rembg(img, alpha_matting=alpha_flag, downscale_max_dim=1600)
            else:
                seg = remove_with_grabcut(img)
            seg = refine_alpha(seg, erode=self.erode_var.get(), dilate=self.dilate_var.get())
            seg = feather_alpha(seg, radius=self.feather_var.get())
            self.segmented = seg
            self.mask = seg.split()[-1].copy()
            self._compose_result(); self._make_preview()
            self.root.after(0, lambda: (self._render_preview(final=True), self._done_process()))
        except Exception as ex:  # pragma: no cover
            self.root.after(0, lambda: (self._set_status(f'Erro: {ex}'), self._done_process()))

    def _done_process(self):
        self.progress.stop()
        self.processing = False
        self._set_status('Pronto')
        
        # Reativar botões após o processamento
        self._toggle_buttons(state=tk.NORMAL)

    def _on_save(self):
        if not self.result:
            messagebox.showinfo('Info','Nada para salvar ainda.'); return
        out = filedialog.asksaveasfilename(defaultextension='.png', filetypes=[('PNG','*.png')])
        if out:
            self.result.save(out, 'PNG'); self._set_status('Salvo')

    def _on_pick_color(self):
        if self.bg_mode_var.get()!='color': self.bg_mode_var.set('color')
        c = colorchooser.askcolor(title='Cor de Fundo')
        if c and c[0]:
            r,g,b = map(int,c[0]); self.bg_color=(r,g,b)
            if self.segmented:
                self._compose_result(); self._make_preview(); self._render_preview()
            self._set_status(f'Cor #{r:02X}{g:02X}{b:02X}')

    # ---------- Preview / Zoom ----------
    def _toggle_adv(self):
        if self.adv_open.get():
            self.adv_panel.pack_forget(); self.adv_open.set(False)
        else:
            self.adv_panel.pack(fill=tk.X, padx=8, pady=(0,4)); self.adv_open.set(True)

    def _make_preview(self):
        src = self.result if self.result else self.orig
        if not src:
            self.preview_scaled=None; self.preview_mask=None; return
        m = max(src.size)
        if m>1600:
            sc = 1600/float(m)
            sz = (int(src.width*sc), int(src.height*sc))
            # Usar LANCZOS para melhor qualidade na pré-visualização inicial
            self.preview_scaled = src.resize(sz, Image.LANCZOS)
            if self.mask:
                self.preview_mask = self.mask.resize(sz, Image.NEAREST)
        else:
            self.preview_scaled = src
            if self.mask:
                self.preview_mask = self.mask.copy()
            else:
                self.preview_mask = None

    def _render_preview(self, final: bool=False):
        if not self.preview_scaled:
            self.canvas.delete('all'); return
            
        # Usar tamanho atual do canvas
        cvs_w = self.canvas.winfo_width() or 800
        cvs_h = self.canvas.winfo_height() or 500
        img = self.preview_scaled
        
        # Cálculo de escala
        if self.fit_var.get():
            scale = min((cvs_w-10)/img.width, (cvs_h-10)/img.height)
            scale = max(0.05, scale)
        else:
            scale = self.zoom_var.get()/100.0
            
        # Otimização: evitar redimensionamento se a escala for próxima a 1.0
        if 0.98 <= scale <= 1.02:
            scale = 1.0
            
        new_size = (max(1,int(img.width*scale)), max(1,int(img.height*scale)))
        
        # Otimização: verificar se o redimensionamento é realmente necessário
        resize_needed = new_size != img.size
        
        # Usar método NEAREST para preview rápido (não-final) para melhorar performance
        resize_method = Image.LANCZOS if final else Image.NEAREST
        disp = img.resize(new_size, resize_method) if resize_needed else img

        # Overlay aprimorado - só processar se necessário
        if self.overlay_var.get() and self.preview_mask is not None and self.mask is not None and self.orig is not None:
            base_orig = self.orig
            if base_orig.size != self.preview_scaled.size:
                base_orig = base_orig.resize(self.preview_scaled.size, Image.BILINEAR)
            if new_size != base_orig.size:
                base_orig = base_orig.resize(new_size, Image.BILINEAR)
            pm = self.preview_mask
            if new_size != pm.size:
                pm = pm.resize(new_size, Image.NEAREST)
            base_rgba = base_orig.convert('RGBA')
            inv = pm.point(lambda a: 255 - a)
            red_alpha = inv.point(lambda a: int(a * 140 / 255))
            overlay_red = Image.new('RGBA', new_size, (255,40,40,0))
            overlay_red.putalpha(red_alpha)
            # contorno verde
            try:
                edge = pm.filter(ImageFilter.MinFilter(3))
                edge = ImageChops.difference(pm, edge).point(lambda v: 255 if v>10 else 0)
                overlay_edge = Image.new('RGBA', new_size, (40,255,80,0))
                overlay_edge.putalpha(edge)
                disp = Image.alpha_composite(base_rgba, overlay_red)
                disp = Image.alpha_composite(disp, overlay_edge)
            except Exception:
                disp = Image.alpha_composite(base_rgba, overlay_red)
        tk_img = ImageTk.PhotoImage(disp)
        self.canvas.delete('all')
        
        # Centralização e posicionamento otimizados
        cx, cy = cvs_w//2, cvs_h//2
        
        # Reset pan se em modo Ajustar
        if self.fit_var.get():
            self._pan_offset = [0,0]
            
        base_ox = cx - new_size[0]//2
        base_oy = cy - new_size[1]//2
        ox = base_ox + self._pan_offset[0]
        oy = base_oy + self._pan_offset[1]
        
        # Limites de deslocamento quando imagem maior que viewport
        if new_size[0] > cvs_w:
            min_ox = cvs_w - new_size[0]
            if ox < min_ox: self._pan_offset[0] += (min_ox - ox); ox = min_ox
            if ox > 0: self._pan_offset[0] -= ox; ox = 0
        else:
            ox = base_ox
            self._pan_offset[0] = ox - base_ox
            
        if new_size[1] > cvs_h:
            min_oy = cvs_h - new_size[1]
            if oy < min_oy: self._pan_offset[1] += (min_oy - oy); oy = min_oy
            if oy > 0: self._pan_offset[1] -= oy; oy = 0
        else:
            oy = base_oy
            self._pan_offset[1] = oy - base_oy
            
        # Criar imagem no canvas
        self.canvas.create_image(ox, oy, image=tk_img, anchor='nw')
        self.canvas.image = tk_img
        self._disp_img_size = new_size
        self._disp_origin = (ox, oy)
        self._update_scrollbars(cvs_w, cvs_h)
        
        # Otimização: aumentar o tempo de debounce e verificar se vale a pena renderizar em alta qualidade
        if not final and resize_needed:
            if self._debounce_hq_id: 
                self.root.after_cancel(self._debounce_hq_id)
            # Aumentar para 300ms para reduzir atualizações
            self._debounce_hq_id = self.root.after(300, lambda: self._render_preview(final=True))

    # ---------- Scrollbars (restaurado) ----------
    def _update_scrollbars(self, cvs_w:int, cvs_h:int):
        iw, ih = self._disp_img_size
        ox, oy = self._disp_origin
        # Horizontal
        if iw > cvs_w:
            left = -ox
            if left < 0: left = 0
            right = left + cvs_w
            self.h_scroll.set(left/iw, min(1.0, right/iw))
            self.h_scroll.state(['!disabled'])
        else:
            self.h_scroll.set(0,1)
            self.h_scroll.state(['disabled'])
        # Vertical
        if ih > cvs_h:
            top = -oy
            if top < 0: top = 0
            bottom = top + cvs_h
            self.v_scroll.set(top/ih, min(1.0, bottom/ih))
            self.v_scroll.state(['!disabled'])
        else:
            self.v_scroll.set(0,1)
            self.v_scroll.state(['disabled'])

    # Corrigir comportamento das barras de rolagem
    def _scroll_x(self, *args):
        if not self._disp_img_size or self.fit_var.get(): return
        iw, ih = self._disp_img_size
        cvs_w = self.canvas.winfo_width() or 1
        if iw <= cvs_w: return
        
        # Ignorar completamente eventos 'moveto' com diferença pequena
        # para evitar movimentos não intencionais ao clicar na barra
        if args[0] == 'moveto':
            # Este é um truque: verificar se viemos de um "clique na barra" ou de um "arrasto"
            # Vamos rastrear o último evento de moveto para diferenciar
            current_time = self.root.tk.call('clock', 'milliseconds')
            last_time = getattr(self, '_last_scroll_x_time', 0)
            last_pos = getattr(self, '_last_scroll_x_pos', None)
            
            self._last_scroll_x_time = current_time
            self._last_scroll_x_pos = float(args[1])
            
            # Se o último evento foi muito recente (menos de 50ms atrás)
            # E houve uma mudança significativa na posição, consideramos um arrasto
            is_drag = (current_time - last_time < 50) and last_pos is not None and abs(float(args[1]) - last_pos) > 0.0001
            
            if not is_drag:
                # Ignorar evento de clique inicial
                return
                
            frac = float(args[1])
        elif args[0] == 'scroll':
            number = int(args[1])
            visible_frac = cvs_w / iw
            step = visible_frac * (1 if args[2]=='pages' else 0.2)
            ox = self._disp_origin[0]
            left = -ox
            cur_frac = left / iw
            frac = cur_frac + number * step
        else:
            return
            
        frac = max(0.0, min(1.0, frac))
        min_ox = cvs_w - iw
        desired_ox = frac * min_ox
        base_ox = (cvs_w - iw)//2
        self._pan_offset[0] = desired_ox - base_ox
        self._render_preview()

    def _scroll_y(self, *args):
        if not self._disp_img_size or self.fit_var.get(): return
        iw, ih = self._disp_img_size
        cvs_h = self.canvas.winfo_height() or 1
        if ih <= cvs_h: return
        
        # Similar ao _scroll_x, evitar movimentos não intencionais
        if args[0] == 'moveto':
            current_time = self.root.tk.call('clock', 'milliseconds')
            last_time = getattr(self, '_last_scroll_y_time', 0)
            last_pos = getattr(self, '_last_scroll_y_pos', None)
            
            self._last_scroll_y_time = current_time
            self._last_scroll_y_pos = float(args[1])
            
            is_drag = (current_time - last_time < 50) and last_pos is not None and abs(float(args[1]) - last_pos) > 0.0001
            
            if not is_drag:
                return
                
            frac = float(args[1])
        elif args[0] == 'scroll':
            number = int(args[1])
            visible_frac = cvs_h / ih
            step = visible_frac * (1 if args[2]=='pages' else 0.2)
            oy = self._disp_origin[1]
            top = -oy
            cur_frac = top / ih
            frac = cur_frac + number * step
        else:
            return
            
        frac = max(0.0, min(1.0, frac))
        min_oy = cvs_h - ih
        desired_oy = frac * min_oy
        base_oy = (cvs_h - ih)//2
        self._pan_offset[1] = desired_oy - base_oy
        self._render_preview()

    def _on_resize(self, _e):
        if not (self.orig or self.result): return
        if self._debounce_resize_id: 
            self.root.after_cancel(self._debounce_resize_id)
        # Aumentar o debounce para evitar múltiplas renderizações durante o redimensionamento
        self._debounce_resize_id = self.root.after(200, lambda: self._render_preview(final=True))

    # ---------- Ferramentas de Máscara ----------
    def _on_brush_change(self):
        mode = self.brush_mode_var.get()
        if mode.startswith('Varinha'):
            self.canvas.config(cursor='target')
        elif mode=='Nenhum':
            self.canvas.config(cursor='')
        else:
            self.canvas.config(cursor='crosshair')

    def _canvas_to_image(self, x:int, y:int) -> Optional[Tuple[int,int]]:
        if not self.result: return None
        ox, oy = self._disp_origin; w,h = self._disp_img_size
        if x<ox or y<oy or x>=ox+w or y>=oy+h: return None
        rx = (x-ox)/float(w); ry=(y-oy)/float(h)
        return (int(self.result.width*rx), int(self.result.height*ry))

    def _on_canvas_press(self, e):
        mode = self.brush_mode_var.get()
        if mode=='Nenhum' or not (self.mask and self.segmented): return
        if mode.startswith('Varinha'):
            self._apply_wand(e.x, e.y, restore=mode.endswith('+'))
        else:
            self._painting=True; self._apply_brush(e.x,e.y)

    def _on_canvas_drag(self, e):
        if self._painting: self._apply_brush(e.x,e.y,dragging=True)

    def _on_canvas_release(self, _e):
        if self._painting:
            self._painting=False
            self._compose_result(); self._make_preview(); self._render_preview(final=True)

    # Pan handlers
    def _on_pan_press(self, e):
        if self.fit_var.get(): return
        self._panning = True
        self._pan_start = (e.x, e.y)
        # snapshot offset
        self._pan_origin = tuple(self._pan_offset)

    def _on_pan_drag(self, e):
        if not self._panning: return
        dx = e.x - self._pan_start[0]
        dy = e.y - self._pan_start[1]
        self._pan_offset[0] = self._pan_origin[0] + dx
        self._pan_offset[1] = self._pan_origin[1] + dy
        self._render_preview()

    def _on_pan_release(self, _e):
        if self._panning:
            self._panning = False
            self._render_preview(final=True)

    def _toggle_overlay(self):
        self.overlay_var.set(not self.overlay_var.get())
        self._render_preview()

    def _apply_brush(self, x:int, y:int, dragging: bool=False):
        if not self.mask: return
        pt = self._canvas_to_image(x,y)
        if not pt: return
        if not dragging: self._push_undo()
        r = max(1,int(self.brush_size_var.get()))
        draw = ImageDraw.Draw(self.mask)
        fill = 255 if self.brush_mode_var.get()=='Recuperar' else 0
        draw.ellipse([pt[0]-r, pt[1]-r, pt[0]+r, pt[1]+r], fill=fill)
        self._compose_result(); self._make_preview(); self._render_preview(final=not dragging)
        self._set_status(f"Pincel {self.brush_mode_var.get()} {r}px")

    # Varinha
    def _apply_wand(self, x:int, y:int, restore: bool):
        if not (self.orig_rgb and self.mask): return
        pt = self._canvas_to_image(x,y)
        if not pt: return
        self._push_undo()
        tol = int(self.wand_tol_var.get())
        region = self._wand_region(pt[0], pt[1], tol)
        if not region:
            self._set_status('Varinha: vazio'); return
        target = 255 if restore else 0
        mload = self.mask.load()
        for (cx,cy) in region:
            mload[cx,cy] = target
        self._compose_result(); self._make_preview(); self._render_preview(final=True)
        self._set_status(f"Varinha {'+' if restore else '-'} {len(region)} px Tol {tol}")

    # Spacebar pan temporary mode
    def _on_space_press(self, _e):
        if not self._space_pan:
            self._space_pan = True
            self.canvas.config(cursor='fleur')
            # switch to pan tool logically (reuse middle button behavior with left button?) could extend later

    def _on_space_release(self, _e):
        if self._space_pan:
            self._space_pan = False
            self._on_brush_change()

    def _wand_region(self, sx:int, sy:int, tol:int) -> List[Tuple[int,int]]:
        base_img = self.orig_lab if self.orig_lab else self.orig_rgb
        img = base_img; w,h = img.size; px = img.load(); seed = px[sx,sy]
        delta_limit = max(1, tol)
        q = deque([(sx,sy)])
        visited = { (sx,sy) }
        region: List[Tuple[int,int]] = []
        sum0=sum1=sum2=0
        limit = min(w*h, 400_000)
        while q and len(region)<limit:
            x,y = q.popleft(); c = px[x,y]
            if region:
                n = len(region); m0=sum0/n; m1=sum1/n; m2=sum2/n
            else:
                m0,m1,m2 = seed
            d_mean = ((c[0]-m0)**2 + (c[1]-m1)**2 + (c[2]-m2)**2) ** 0.5
            d_seed = ((c[0]-seed[0])**2 + (c[1]-seed[1])**2 + (c[2]-seed[2])**2) ** 0.5
            if d_mean <= delta_limit and d_seed <= delta_limit*1.4:
                region.append((x,y))
                sum0+=c[0]; sum1+=c[1]; sum2+=c[2]
                if x>0 and (x-1,y) not in visited: visited.add((x-1,y)); q.append((x-1,y))
                if x<w-1 and (x+1,y) not in visited: visited.add((x+1,y)); q.append((x+1,y))
                if y>0 and (x,y-1) not in visited: visited.add((x,y-1)); q.append((x,y-1))
                if y<h-1 and (x,y+1) not in visited: visited.add((x,y+1)); q.append((x,y+1))
        return region

    # Undo
    def _push_undo(self):
        if self.mask:
            if len(self._undo_stack)>=8: self._undo_stack.pop(0)
            self._undo_stack.append(self.mask.copy())

    def _undo_mask(self):
        if not self._undo_stack:
            self._set_status('Undo vazio'); return
        self.mask = self._undo_stack.pop()
        self._compose_result(); self._make_preview(); self._render_preview(final=True)
        self._set_status('Undo')

    def _compose_result(self):
        if not self.segmented: return
        base = self.segmented
        if self.mask and self.mask.size == base.size:
            r,g,b,_ = base.split(); base = Image.merge('RGBA',(r,g,b,self.mask))
        if self.bg_mode_var.get()=='color' and self.bg_color:
            self.result = replace_background(base, bg_color=self.bg_color + (255,))
        else:
            self.result = base

    # ---------- Helpers ----------
    def _resolve_method(self) -> str:
        m = self.mode_var.get()
        if m=='Auto': return 'rembg' if HAS_REMBG else 'grabcut'
        if m=='Qualidade':
            if HAS_REMBG: return 'rembg'
            self._set_status('rembg ausente; usando GrabCut'); self.mode_var.set('Rápido'); return 'grabcut'
        return 'grabcut'

    def _sync_mode(self):
        self._set_status(f"Modo: {self.mode_var.get()}")

    def _set_status(self, msg:str):
        self.status_var.set(msg); self.root.update_idletasks()

    def _toggle_buttons(self, state):
        """Ativa ou desativa botões principais da interface."""
        for widget in self.root.winfo_children():
            if isinstance(widget, ttk.Button):
                widget.config(state=state)

    # Método ausente que precisa ser adicionado
    def _on_wheel(self, event):
        # Ctrl + roda = zoom; sem Ctrl = rolagem; Shift = horizontal
        ctrl = (event.state & 0x4) != 0
        shift = (event.state & 0x1) != 0
        if ctrl:
            if self.fit_var.get():
                self.fit_var.set(False)
            old_zoom = self.zoom_var.get()
            step = 8 if event.delta>0 else -8
            new_zoom = max(10, min(400, old_zoom + step))
            if new_zoom == old_zoom: return
            if self._disp_img_size[0] and self._disp_img_size[1]:
                ox, oy = self._disp_origin
                iw, ih = self._disp_img_size
                mx, my = event.x, event.y
                if ox <= mx <= ox+iw and oy <= my <= oy+ih:
                    relx = (mx - ox)/iw; rely = (my - oy)/ih
                else:
                    relx = rely = 0.5
            else:
                relx = rely = 0.5
            self.zoom_var.set(new_zoom)
            ratio = new_zoom / old_zoom if old_zoom else 1
            self._pan_offset[0] = int((self._pan_offset[0] + (relx-0.5)*self._disp_img_size[0]) * ratio - (relx-0.5)*(self._disp_img_size[0]*ratio))
            self._pan_offset[1] = int((self._pan_offset[1] + (rely-0.5)*self._disp_img_size[1]) * ratio - (rely-0.5)*(self._disp_img_size[1]*ratio))
            self._render_preview()
            return
        # Scroll
        if self.fit_var.get():
            # sair do modo ajustar para permitir rolagem quando imagem maior
            self.fit_var.set(False)
        delta_units = int(event.delta/120)
        scroll_px = -delta_units * 60  # invertido para sensação natural
        if shift:
            self._pan_offset[0] += scroll_px
        else:
            self._pan_offset[1] += scroll_px
        self._render_preview()

    def _on_zoom(self):
        if self.fit_var.get(): return
        self._render_preview()

    def _set_zoom(self, p:int):
        self.fit_var.set(False); self.zoom_var.set(p); self._render_preview()

    def _fit(self):
        self.fit_var.set(True); self._render_preview(final=True)

    def _toggle_fit(self):
        self.fit_var.set(not self.fit_var.get()); self._render_preview()

def main():
    root = tk.Tk(); FundoZeroGUI(root); root.mainloop()


if __name__ == '__main__':
    main()
