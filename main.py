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
import json

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
CONFIG_PATH = Path.home() / '.fundozero_config.json'


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
        # NOVO: sensibilidade (0 preserva mais, 100 remove mais, 50 neutro)
        self.sensitivity_var = tk.IntVar(value=50)
        self.adv_open = tk.BooleanVar(value=False)
        self.alpha_matting_var = tk.BooleanVar(value=False)
        self.brush_mode_var = tk.StringVar(value='Nenhum')
        self.brush_size_var = tk.IntVar(value=30)
        self.wand_tol_var = tk.IntVar(value=18)
        # NOVO: raio da varinha (limita área de atuação)
        self.wand_radius_var = tk.IntVar(value=100)
        self.overlay_var = tk.BooleanVar(value=False)
        self._undo_stack: List[Image.Image] = []
        # Persistência
        self._save_after_id = None
        self._loading_config = False
        # Base sem sensibilidade (para reaplicar sem reprocesar pesado)
        self.segmented_base: Optional[Image.Image] = None
        self._mask_edited = False

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
        # Pré-visualização de ferramenta
        self._cursor_overlay_tag = 'cursor_overlay'
        self._last_mouse: Optional[Tuple[int,int]] = None

        self._build()
        # Persistência: carregar após construção de variáveis/UI
        self._load_config()
        self._attach_traces()
        # Salvar ao fechar
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.bind('<Configure>', self._on_resize)
        self._set_status('Abra uma imagem')
        self.root.bind('m', lambda _e: self._toggle_overlay())
        self.root.bind('<KeyPress-space>', self._on_space_press)
        self.root.bind('<KeyRelease-space>', self._on_space_release)

    # --- Persistência ---
    def _attach_traces(self):
        vars_to_watch = [
            self.feather_var, self.erode_var, self.dilate_var,
            self.brush_size_var, self.wand_tol_var,
            self.mode_var, self.bg_mode_var,
            self.sensitivity_var, self.wand_radius_var  # novo
        ]
        for v in vars_to_watch:
            v.trace_add('write', lambda *_a: self._schedule_save())
        # Atualização de labels de valor
        for name, (var, lbl) in getattr(self, '_value_labels', {}).items():
            var.trace_add('write', lambda *_a, v=var, l=lbl: l.config(text=str(v.get())))
        # Sensibilidade: reaplicar se possível
        self.sensitivity_var.trace_add('write', lambda *_a: self._on_sensitivity_change())
        # Atualizar overlay ao mudar tamanho do pincel
        self.brush_size_var.trace_add('write', lambda *_a: self._draw_cursor_overlay())

    def _schedule_save(self):
        if self._loading_config:
            return
        if self._save_after_id:
            self.root.after_cancel(self._save_after_id)
        self._save_after_id = self.root.after(400, self._save_config)

    def _load_config(self):
        if not CONFIG_PATH.exists():
            return
        try:
            self._loading_config = True
            data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
            def set_if(key, var):
                if key in data:
                    try: var.set(data[key])
                    except Exception: pass
            set_if('feather', self.feather_var)
            set_if('erode', self.erode_var)
            set_if('dilate', self.dilate_var)
            set_if('brush_size', self.brush_size_var)
            set_if('wand_tol', self.wand_tol_var)
            set_if('wand_radius', self.wand_radius_var)  # novo
            set_if('mode', self.mode_var)
            set_if('bg_mode', self.bg_mode_var)
            set_if('sensitivity', self.sensitivity_var)  # novo
            if 'bg_color' in data and isinstance(data['bg_color'], list) and len(data['bg_color'])==3:
                self.bg_color = tuple(data['bg_color'])
            # Atualizar preview se já houver imagem
            if self.orig:
                self._compose_result(); self._make_preview(); self._render_preview()
        except Exception:
            pass
        finally:
            self._loading_config = False

    def _save_config(self):
        cfg = {
            'feather': self.feather_var.get(),
            'erode': self.erode_var.get(),
            'dilate': self.dilate_var.get(),
            'brush_size': self.brush_size_var.get(),
            'wand_tol': self.wand_tol_var.get(),
            'wand_radius': self.wand_radius_var.get(),  # novo
            'mode': self.mode_var.get(),
            'bg_mode': self.bg_mode_var.get(),
            'bg_color': list(self.bg_color) if self.bg_color else None,
            'sensitivity': self.sensitivity_var.get(),  # novo
        }
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding='utf-8')
        except Exception:
            pass

    def _on_close(self):
        self._save_config()
        self.root.destroy()

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
            # NOVO: controle de raio da varinha
            ttk.Label(top, text='Raio Var:').pack(side=tk.LEFT, padx=(6,2))
            ttk.Scale(top, from_=10, to=500, variable=self.wand_radius_var, orient='horizontal', length=90).pack(side=tk.LEFT, padx=2)
            ttk.Spinbox(top, from_=10, to=500, textvariable=self.wand_radius_var, width=5, wrap=True).pack(side=tk.LEFT, padx=2)
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
            self.canvas.bind('<Motion>', self._on_canvas_motion)  # NOVO
            self.canvas.bind('<Double-Button-1>', lambda _e: self._toggle_fit())
            self.canvas.bind('<ButtonPress-1>', self._on_canvas_press)
            self.canvas.bind('<B1-Motion>', self._on_canvas_drag)
            self.canvas.bind('<ButtonRelease-1>', self._on_canvas_release)
            self.canvas.bind('<ButtonPress-2>', self._on_pan_press)
            self.canvas.bind('<B2-Motion>', self._on_pan_drag)
            self.canvas.bind('<ButtonRelease-2>', self._on_pan_release)

            # Painel avançado (novo layout explicativo)
            self.adv_panel = ttk.Frame(self.root)
            self._value_labels = {}
            def add_param(text, desc, var, from_, to_):
                row = ttk.LabelFrame(self.adv_panel, text=text)
                row.pack(fill='x', pady=4)
                top_line = ttk.Frame(row)
                top_line.pack(fill='x', padx=4, pady=(2,0))
                ttk.Label(top_line, text=desc, foreground='#666').pack(side='left')
                val_lbl = ttk.Label(top_line, text=str(var.get()), width=4, anchor='e')
                val_lbl.pack(side='right')
                self._value_labels[text] = (var, val_lbl)
                body = ttk.Frame(row)
                body.pack(fill='x', padx=4, pady=2)
                scale = ttk.Scale(body, from_=from_, to_=to_, variable=var, orient='horizontal')
                scale.pack(side='left', fill='x', expand=True, padx=(0,6))
                spin = ttk.Spinbox(body, from_=from_, to_=to_, textvariable=var, width=5, wrap=True)
                spin.pack(side='right')
            # NOVO: Sensibilidade primeiro
            add_param('Sensibilidade', 'Agressividade da remoção (baixo=preserva)', self.sensitivity_var, 0, 100)
            add_param('Suavização', 'Transição suave das bordas (feather)', self.feather_var, 0, 12)
            add_param('Contrair', 'Remove pixels da borda (erode)', self.erode_var, 0, 10)
            add_param('Expandir', 'Adiciona pixels à borda (dilate)', self.dilate_var, 0, 10)
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
            # Base antes da sensibilidade
            self.segmented_base = seg.copy()
            seg = self._apply_sensitivity_to_image(seg, self.sensitivity_var.get())
            self.segmented = seg
            self.mask = seg.split()[-1].copy()
            self._mask_edited = False
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
            self._schedule_save()

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
        # Redesenhar overlay após atualizar imagem
        self._draw_cursor_overlay()

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

    # ---------- Sensibilidade ----------
    def _on_sensitivity_change(self):
        if self._loading_config:
            return
        if self.segmented_base and not self._mask_edited:
            self._reapply_sensitivity()
        elif self.segmented_base and self._mask_edited:
            self._set_status('Sensibilidade alterada: reprocessar para aplicar (Processar)')

    def _reapply_sensitivity(self):
        if not self.segmented_base:
            return
        seg = self._apply_sensitivity_to_image(self.segmented_base.copy(), self.sensitivity_var.get())
        self.segmented = seg
        self.mask = seg.split()[-1].copy()
        self._compose_result(); self._make_preview(); self._render_preview()
        self._set_status(f"Sensibilidade: {self.sensitivity_var.get()}")

    def _apply_sensitivity_to_image(self, img: Image.Image, sens: int) -> Image.Image:
        """Ajusta o canal alpha para mais ou menos remoção.
        sens=50 neutro; >50 remove mais (encolhe), <50 preserva mais (expande)."""
        try:
            sens = max(0, min(100, int(sens)))
            a = img.split()[-1]
            offset = sens - 50
            if offset == 0:
                return img
            if offset > 0:
                # Mais remoção: aumenta threshold e comprime faixa
                thr = 128 + int((offset/50)*40)  # 128..168
                lut = []
                for v in range(256):
                    if v < thr:
                        lut.append(0)
                    else:
                        lut.append(min(255, int((v - thr) / (255 - thr) * 255)))
                a = a.point(lut)
                # leve erosão adicional proporcional
                if offset > 25:
                    try:
                        a = a.filter(ImageFilter.MinFilter(3))
                    except Exception:
                        pass
            else:
                # Menos remoção: reduz threshold e expande
                pos = -offset
                thr = 128 - int((pos/50)*40)  # 128..88
                lut = []
                for v in range(256):
                    if v > thr:
                        lut.append(255)
                    else:
                        lut.append(int(v / max(1, thr) * 255))
                a = a.point(lut)
                # leve dilatação adicional
                if pos > 25:
                    try:
                        a = a.filter(ImageFilter.MaxFilter(3))
                    except Exception:
                        pass
            r,g,b,_ = img.split()
            return Image.merge('RGBA', (r,g,b,a))
        except Exception:
            return img

    # ---------- Ferramentas de Máscara ----------
    def _on_brush_change(self):
        mode = self.brush_mode_var.get()
        if mode.startswith('Varinha'):
            self.canvas.config(cursor='target')
        elif mode=='Nenhum':
            self.canvas.config(cursor='')
        else:
            self.canvas.config(cursor='crosshair')
        self._draw_cursor_overlay()  # atualizar preview

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
        # atualizar overlay (caso clique sem mover)
        self._draw_cursor_overlay()

    def _on_canvas_drag(self, e):
        if self._painting: self._apply_brush(e.x,e.y,dragging=True)

    def _on_canvas_release(self, _e):
        if self._painting:
            self._painting=False
            self._compose_result(); self._make_preview(); self._render_preview(final=True)
        self._draw_cursor_overlay()

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
        self._draw_cursor_overlay()

    def _on_pan_release(self, _e):
        if self._panning:
            self._panning = False
            self._render_preview(final=True)
            self._draw_cursor_overlay()

    def _toggle_overlay(self):
        self.overlay_var.set(not self.overlay_var.get())
        self._render_preview()

    def _apply_brush(self, x:int, y:int, dragging: bool=False):
        if not self.mask: return
        pt = self._canvas_to_image(x,y)
        if not pt: return
        if not dragging: self._mask_edited = True
        r = max(1,int(self.brush_size_var.get()))
        draw = ImageDraw.Draw(self.mask)
        fill = 255 if self.brush_mode_var.get()=='Recuperar' else 0
        draw.ellipse([pt[0]-r, pt[1]-r, pt[0]+r, pt[1]+r], fill=fill)
        self._compose_result(); self._make_preview(); self._render_preview(final=not dragging)
        self._set_status(f"Pincel {self.brush_mode_var.get()} {r}px")
        self._draw_cursor_overlay()

    # Varinha
    def _apply_wand(self, x:int, y:int, restore: bool):
        if not (self.orig_rgb and self.mask and self.segmented): return
        pt = self._canvas_to_image(x,y)
        if not pt: return
        self._push_undo()
        tol = int(self.wand_tol_var.get())
        radius = int(self.wand_radius_var.get())
        region = self._wand_region(pt[0], pt[1], tol, max_radius=radius)
        if not region:
            self._set_status('Varinha: vazio'); return
            
        # Obter canal alpha original para preservar transparências suaves
        original_alpha = self.segmented.split()[-1] if self.segmented else None
        mload = self.mask.load()
        
        if restore and original_alpha:
            # Ao recuperar, usar os valores originais de transparência
            aload = original_alpha.load()
            for (cx,cy) in region:
                mload[cx,cy] = aload[cx,cy]  # Preservar valor original de transparência
        else:
            # Ao remover, definir como transparente
            target = 0
            for (cx,cy) in region:
                mload[cx,cy] = target
                
        self._compose_result(); self._make_preview(); self._render_preview(final=True)
        self._set_status(f"Varinha {'+' if restore else '-'} {len(region)} px Tol {tol}")
        self._draw_cursor_overlay()

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

    def _wand_region(self, sx:int, sy:int, tol:int, max_radius: int = 100) -> List[Tuple[int,int]]:
        base_img = self.orig_lab if self.orig_lab else self.orig_rgb
        img = base_img; w,h = img.size; px = img.load(); seed = px[sx,sy]
        delta_limit = max(1, tol)
        q = deque([(sx,sy)])
        visited = { (sx,sy) }
        region: List[Tuple[int,int]] = []
        sum0=sum1=sum2=0
        limit = min(w*h, 400_000)
        while q and len(region)<limit:
            x,y = q.popleft()
            # NOVO: verificar distância do seed
            dist = ((x - sx)**2 + (y - sy)**2)**0.5
            if dist > max_radius:
                continue
            c = px[x,y]
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
        self._mask_edited = True

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
            self._draw_cursor_overlay()
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
        self._draw_cursor_overlay()

    def _on_zoom(self):
        if self.fit_var.get(): return
        self._render_preview()
        self._draw_cursor_overlay()

    def _set_zoom(self, p:int):
        self.fit_var.set(False); self.zoom_var.set(p); self._render_preview()

    def _fit(self):
        self.fit_var.set(True); self._render_preview(final=True)

    def _toggle_fit(self):
        self.fit_var.set(not self.fit_var.get()); self._render_preview()
        self._draw_cursor_overlay()

    def _on_brush_change(self):
        mode = self.brush_mode_var.get()
        if mode.startswith('Varinha'):
            self.canvas.config(cursor='target')
        elif mode=='Nenhum':
            self.canvas.config(cursor='')
        else:
            self.canvas.config(cursor='crosshair')
        self._draw_cursor_overlay()  # atualizar preview

    def _on_canvas_motion(self, e):
        """Rastreia posição do mouse e atualiza indicador da ferramenta."""
        self._last_mouse = (e.x, e.y)
        self._draw_cursor_overlay()

    def _clear_cursor_overlay(self):
        self.canvas.delete(self._cursor_overlay_tag)

    def _draw_cursor_overlay(self):
        """Desenha círculo (pincel) ou alvo (varinha) indicando área de atuação."""
        self._clear_cursor_overlay()
        if not self._last_mouse:
            return
        mode = self.brush_mode_var.get()
        if mode == 'Nenhum':
            return
        if not self._disp_img_size or (not self.result and not self.orig):
            return
        x, y = self._last_mouse
        ox, oy = self._disp_origin
        iw, ih = self._disp_img_size
        # Dentro da imagem renderizada?
        if not (ox <= x <= ox+iw and oy <= y <= oy+ih):
            return
        # Escala para converter raio imagem -> display
        base_img = self.result if self.result else self.orig
        if not base_img:
            return
        scale_x = iw / base_img.width
        scale_y = ih / base_img.height
        # Ferramentas de pincel
        if mode in ('Recuperar','Remover'):
            r_img = max(1, int(self.brush_size_var.get()))
            r_disp = max(2, int(r_img * (scale_x + scale_y)/2.0))
            color = '#31ff7a' if mode=='Recuperar' else '#ff4d4d'
            self.canvas.create_oval(
                x-r_disp, y-r_disp, x+r_disp, y+r_disp,
                outline=color, width=2, dash=(5,3), tags=self._cursor_overlay_tag
            )
            self.canvas.create_text(
                x, y+r_disp+12,
                text=f'{r_img}px',
                fill=color,
                font=('TkDefaultFont', 9, 'bold'),
                tags=self._cursor_overlay_tag
            )
        elif mode.startswith('Varinha'):
            # Pequeno alvo central + círculo de raio
            size = 14
            color = '#ffd94a'
            self.canvas.create_oval(
                x-size//2, y-size//2, x+size//2, y+size//2,
                outline=color, width=2, tags=self._cursor_overlay_tag
            )
            self.canvas.create_line(x-8, y, x+8, y, fill=color, width=1, tags=self._cursor_overlay_tag)
            self.canvas.create_line(x, y-8, x, y+8, fill=color, width=1, tags=self._cursor_overlay_tag)
            # NOVO: círculo de raio da varinha
            r_img = max(10, int(self.wand_radius_var.get()))
            r_disp = max(2, int(r_img * (scale_x + scale_y)/2.0))
            self.canvas.create_oval(
                x-r_disp, y-r_disp, x+r_disp, y+r_disp,
                outline=color, width=1, dash=(3,2), tags=self._cursor_overlay_tag
            )
            self.canvas.create_text(
                x, y+r_disp+12,
                text=f'{r_img}px',
                fill=color,
                font=('TkDefaultFont', 8),
                tags=self._cursor_overlay_tag
            )

def main():
    root = tk.Tk(); FundoZeroGUI(root); root.mainloop()


if __name__ == '__main__':
    main()
