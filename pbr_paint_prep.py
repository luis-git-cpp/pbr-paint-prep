# -*- coding: utf-8 -*-
"""
PBR Paint Prep  -  Blender 4.x / 5.x

COMO USAR
1. Importe o modelo (ex.: saída do TripoSR), selecione-o e abra este arquivo no
   editor de texto do Blender (Scripting > Open). Rode com Alt+P.
2. No viewport aperte N: aparece a aba "PBR" com 3 botões.
   1) Preparar    -> cria low-poly + UV + texturas + material + bake (normal, AO,
                     cor de vértice -> albedo) e entra no modo de pintura.
   2) Modo pintura-> volta para o Texture Paint (troque de textura em "Texture Slots").
   3) Finalizar   -> salva tudo, empacota ORM (AO/Roughness/Metallic) e exporta o GLB.

Pinte: albedo, roughness, metallic (e emissive, se quiser).
Não pinte: normal e AO (vêm do bake). O ORM só é montado no passo 3.
"""
import bpy
import math
import traceback
import addon_utils
import numpy as np
from pathlib import Path

# ============================ CONFIGURAÇÃO ===============================
NOME_BASE = None        # None = usa o nome do objeto selecionado
TEX_SIZE = 2048         # 1024 / 2048 / 4096
FACES_ALVO = 15000      # triângulos da low-poly (0 = não decimar)
FAZER_BAKE = True       # bake high -> low (normal, AO, cor de vértice -> albedo)
CAGE_EXTRUSAO = 0.02    # fração do maior tamanho do modelo (aumente se o bake falhar)
MARGEM_PX = 16          # padding (bleed) das ilhas de UV no bake
AMOSTRAS_AO = 32
INCLUIR_EMISSAO = True  # se o emissive ficar todo preto, é removido no export
USAR_GPU_AMD = True     # tenta HIP no bake; se não achar GPU, usa CPU
# =========================================================================

CANAIS = ("albedo", "roughness", "metallic", "ao", "normal")


# ------------------------------ utilidades -------------------------------
def selecionar(objs, ativo=None):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    for o in objs:
        o.select_set(True)
    bpy.context.view_layer.objects.active = ativo or objs[0]


def modo_objeto():
    ob = bpy.context.view_layer.objects.active
    if ob and ob.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')


def triangulos(obj):
    return sum(len(p.vertices) - 2 for p in obj.data.polygons)


def nome_do(obj):
    return NOME_BASE or obj.name.replace("_HIGH", "")


def pasta_saida():
    if bpy.data.filepath:
        base = Path(bpy.path.abspath("//"))
    else:
        base = Path.home() / "blender_pbr"
    pasta = base / "texturas"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def habilitar_addons():
    """Ativa os add-ons embutidos úteis (se existirem nesta versão)."""
    for mod in ("io_scene_gltf2", "node_wrangler"):
        try:
            addon_utils.enable(mod, default_set=True, persistent=True,
                               handle_error=lambda e: None)
        except Exception as e:
            print(f"[aviso] add-on {mod} não ativado: {e}")


def duplicar(obj, nome):
    novo = obj.copy()
    novo.data = obj.data.copy()
    novo.name = nome
    for c in obj.users_collection:
        c.objects.link(novo)
    return novo


# ------------------------------- malha -----------------------------------
def decimar(obj, alvo):
    tris = triangulos(obj)
    if alvo and tris > alvo:
        m = obj.modifiers.new("Decimate", 'DECIMATE')
        m.decimate_type = 'COLLAPSE'
        m.ratio = alvo / tris
        m.use_collapse_triangulate = True
        selecionar([obj])
        bpy.ops.object.modifier_apply(modifier=m.name)


def limpar_malha(obj):
    selecionar([obj])
    bpy.ops.object.mode_set(mode='EDIT')
    bpy.ops.mesh.select_all(action='SELECT')
    bpy.ops.mesh.remove_doubles(threshold=1e-5)
    bpy.ops.mesh.normals_make_consistent(inside=False)
    bpy.ops.object.mode_set(mode='OBJECT')
    me = obj.data
    me.polygons.foreach_set("use_smooth", [True] * len(me.polygons))
    me.update()


def desdobrar_uv(obj, recriar):
    me = obj.data
    if recriar:
        while me.uv_layers:
            me.uv_layers.remove(me.uv_layers[0])
    if not me.uv_layers:
        selecionar([obj])
        bpy.ops.object.mode_set(mode='EDIT')
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.smart_project(angle_limit=math.radians(66),
                                 island_margin=8.0 / TEX_SIZE)
        bpy.ops.object.mode_set(mode='OBJECT')
    me.uv_layers.active.name = "UVMap"


# ----------------------------- texturas/material -------------------------
def criar_imagens(nome, pasta):
    specs = {
        "albedo": ((0.8, 0.8, 0.8, 1), False),
        "roughness": ((0.5, 0.5, 0.5, 1), True),
        "metallic": ((0.0, 0.0, 0.0, 1), True),
        "ao": ((1.0, 1.0, 1.0, 1), True),
        "normal": ((0.5, 0.5, 1.0, 1), True),
    }
    if INCLUIR_EMISSAO:
        specs["emissive"] = ((0.0, 0.0, 0.0, 1), False)
    imgs = {}
    for k, (cor, dados) in specs.items():
        n = f"{nome}_{k}"
        antiga = bpy.data.images.get(n)
        if antiga:
            bpy.data.images.remove(antiga)
        img = bpy.data.images.new(n, TEX_SIZE, TEX_SIZE, alpha=False, is_data=dados)
        img.generated_color = cor
        img.filepath_raw = str(pasta / f"{n}.png")
        img.file_format = 'PNG'
        imgs[k] = img
    return imgs


def grupo_gltf():
    """Grupo 'glTF Material Output' que o exportador lê para o Occlusion."""
    nome = "glTF Material Output"
    g = bpy.data.node_groups.get(nome)
    if g:
        return g
    g = bpy.data.node_groups.new(nome, 'ShaderNodeTree')
    if hasattr(g, "interface"):
        g.interface.new_socket("Occlusion", in_out='INPUT', socket_type='NodeSocketFloat')
    else:
        g.inputs.new('NodeSocketFloat', "Occlusion")
    return g


def construir_material(nome, imgs, empacotado=False):
    antigo = bpy.data.materials.get(nome)
    if antigo:
        bpy.data.materials.remove(antigo)
    mat = bpy.data.materials.new(nome)
    try:
        mat.use_nodes = True
    except Exception:
        pass
    nt = mat.node_tree
    nt.nodes.clear()
    L = nt.links.new

    def no(tipo, x, y):
        n = nt.nodes.new(tipo)
        n.location = (x, y)
        return n

    def tex(img, x, y):
        n = no('ShaderNodeTexImage', x, y)
        n.image = img
        n.label = img.name
        return n

    saida = no('ShaderNodeOutputMaterial', 700, 0)
    bsdf = no('ShaderNodeBsdfPrincipled', 400, 0)
    L(bsdf.outputs['BSDF'], saida.inputs['Surface'])

    L(tex(imgs["albedo"], -300, 600).outputs['Color'], bsdf.inputs['Base Color'])

    nmap = no('ShaderNodeNormalMap', 100, -500)
    nmap.space = 'TANGENT'
    nmap.uv_map = "UVMap"
    L(tex(imgs["normal"], -300, -500).outputs['Color'], nmap.inputs['Color'])
    L(nmap.outputs['Normal'], bsdf.inputs['Normal'])

    if empacotado:
        sep = no('ShaderNodeSeparateColor', 0, 100)
        L(tex(imgs["orm"], -300, 100).outputs['Color'], sep.inputs['Color'])
        L(sep.outputs['Green'], bsdf.inputs['Roughness'])
        L(sep.outputs['Blue'], bsdf.inputs['Metallic'])
        grp = no('ShaderNodeGroup', 400, -400)
        grp.node_tree = grupo_gltf()
        L(sep.outputs['Red'], grp.inputs['Occlusion'])
    else:
        L(tex(imgs["roughness"], -300, 300).outputs['Color'], bsdf.inputs['Roughness'])
        L(tex(imgs["metallic"], -300, 0).outputs['Color'], bsdf.inputs['Metallic'])
        tex(imgs["ao"], -300, -250)  # sem ligação: só como slot (bake/visualização)

    if "emissive" in imgs:
        ent = bsdf.inputs.get('Emission Color') or bsdf.inputs.get('Emission')
        L(tex(imgs["emissive"], -300, -750).outputs['Color'], ent)
        if bsdf.inputs.get('Emission Strength'):
            bsdf.inputs['Emission Strength'].default_value = 1.0
    return mat


# --------------------------------- bake ----------------------------------
def configurar_cycles():
    sc = bpy.context.scene
    sc.render.engine = 'CYCLES'
    sc.cycles.device = 'CPU'
    if USAR_GPU_AMD:
        try:
            prefs = bpy.context.preferences.addons['cycles'].preferences
            prefs.compute_device_type = 'HIP'
            atualizar = getattr(prefs, "refresh_devices", None) or prefs.get_devices
            atualizar()
            achou = False
            for d in prefs.devices:
                d.use = (d.type == 'HIP')
                achou = achou or d.use
            if achou:
                sc.cycles.device = 'GPU'
            else:
                for d in prefs.devices:
                    d.use = (d.type == 'CPU')
        except Exception as e:
            print(f"[aviso] GPU HIP indisponível, usando CPU: {e}")
    sc.cycles.use_denoising = False
    b = sc.render.bake
    b.target = 'IMAGE_TEXTURES'
    b.use_selected_to_active = True
    b.margin = MARGEM_PX
    b.margin_type = 'EXTEND'
    b.normal_space = 'TANGENT'
    b.max_ray_distance = 0.0
    b.use_clear = False


def bake_passo(high, low, mat, img, tipo, amostras=1):
    nt = mat.node_tree
    alvo = next(n for n in nt.nodes if n.type == 'TEX_IMAGE' and n.image == img)
    for n in nt.nodes:
        n.select = False
    alvo.select = True
    nt.nodes.active = alvo
    bpy.context.scene.cycles.samples = amostras
    selecionar([high, low], low)
    bpy.ops.object.bake(type=tipo)


def material_cor_vertice(attr):
    mat = bpy.data.materials.new("_tmp_cor_vertice")
    try:
        mat.use_nodes = True
    except Exception:
        pass
    nt = mat.node_tree
    nt.nodes.clear()
    vc = nt.nodes.new('ShaderNodeVertexColor')
    vc.layer_name = attr
    em = nt.nodes.new('ShaderNodeEmission')
    out = nt.nodes.new('ShaderNodeOutputMaterial')
    nt.links.new(vc.outputs['Color'], em.inputs['Color'])
    nt.links.new(em.outputs['Emission'], out.inputs['Surface'])
    return mat


def fazer_bake(high, low, mat, imgs):
    configurar_cycles()
    ext = max(high.dimensions) * CAGE_EXTRUSAO
    bpy.context.scene.render.bake.cage_extrusion = ext

    bake_passo(high, low, mat, imgs["normal"], 'NORMAL')
    bake_passo(high, low, mat, imgs["ao"], 'AO', AMOSTRAS_AO)

    cores = high.data.color_attributes
    if len(cores) == 0:
        print("[aviso] a high-poly não tem cor de vértice: albedo fica cinza.")
        return
    attr = cores.active_color or cores[0]
    originais = list(high.data.materials)
    tmp = material_cor_vertice(attr.name)
    high.data.materials.clear()
    high.data.materials.append(tmp)
    try:
        bake_passo(high, low, mat, imgs["albedo"], 'EMIT')
    finally:
        high.data.materials.clear()
        for m in originais:
            high.data.materials.append(m)
        bpy.data.materials.remove(tmp)


# ------------------------------ pintura ----------------------------------
def entrar_modo_pintura(obj, mat, imgs):
    selecionar([obj])
    try:
        ts = bpy.context.scene.tool_settings
        ts.image_paint.mode = 'MATERIAL'
        ts.image_paint.seam_bleed = 4
        lista = list(mat.texture_paint_images)
        if imgs["albedo"] in lista:
            mat.paint_active_slot = lista.index(imgs["albedo"])
        bpy.ops.object.mode_set(mode='TEXTURE_PAINT')
    except Exception as e:
        print(f"[aviso] não consegui entrar no Texture Paint: {e}")
    try:
        for area in bpy.context.screen.areas:
            if area.type == 'VIEW_3D':
                area.spaces[0].shading.type = 'MATERIAL'
    except Exception:
        pass


# ------------------------------ fluxo principal --------------------------
def preparar(obj):
    habilitar_addons()
    modo_objeto()
    sc = bpy.context.scene
    sc.view_settings.view_transform = 'Standard'  # cores iguais às do viewer glTF

    nome = nome_do(obj)
    pasta = pasta_saida()

    selecionar([obj])
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

    if FAZER_BAKE:
        high = obj
        high.name = f"{nome}_HIGH"
        low = duplicar(high, nome)
        decimar(low, FACES_ALVO)
        for a in list(low.data.color_attributes):   # cor vai para a textura
            low.data.color_attributes.remove(a)
        limpar_malha(low)
        desdobrar_uv(low, recriar=True)
    else:
        high, low = None, obj
        limpar_malha(low)
        desdobrar_uv(low, recriar=False)

    imgs = criar_imagens(nome, pasta)
    mat = construir_material(f"{nome}_PINTURA", imgs)
    low.data.materials.clear()
    low.data.materials.append(mat)

    if FAZER_BAKE:
        fazer_bake(high, low, mat, imgs)
        high.hide_set(True)
        high.hide_render = True

    for img in imgs.values():
        img.save()
    entrar_modo_pintura(low, mat, imgs)
    return low


def canal_r(img, n):
    a = np.empty(n * 4, dtype=np.float32)
    img.pixels.foreach_get(a)
    return a.reshape(-1, 4)[:, 0]


def finalizar(obj):
    modo_objeto()
    nome = nome_do(obj)
    pasta = pasta_saida()
    imgs = {k: bpy.data.images.get(f"{nome}_{k}") for k in CANAIS + ("emissive",)}
    faltando = [k for k in CANAIS if imgs[k] is None]
    if faltando:
        raise RuntimeError(f"Texturas não encontradas: {faltando}. Rode o passo 1.")

    w, h = imgs["ao"].size
    n = w * h
    for img in imgs.values():
        if img:
            img.save()

    # --- empacota ORM: R = AO, G = Roughness, B = Metallic -------------
    orm = np.ones((n, 4), dtype=np.float32)
    orm[:, 0] = canal_r(imgs["ao"], n)
    orm[:, 1] = canal_r(imgs["roughness"], n)
    orm[:, 2] = canal_r(imgs["metallic"], n)
    nome_orm = f"{nome}_ORM"
    antiga = bpy.data.images.get(nome_orm)
    if antiga:
        bpy.data.images.remove(antiga)
    img_orm = bpy.data.images.new(nome_orm, w, h, alpha=False, is_data=True)
    img_orm.pixels.foreach_set(orm.ravel())
    img_orm.update()
    img_orm.filepath_raw = str(pasta / f"{nome_orm}.png")
    img_orm.file_format = 'PNG'
    img_orm.save()

    # --- emissive todo preto? não exporta ----------------------------
    exp = {"albedo": imgs["albedo"], "normal": imgs["normal"], "orm": img_orm}
    em = imgs.get("emissive")
    if em is not None:
        a = np.empty(n * 4, dtype=np.float32)
        em.pixels.foreach_get(a)
        if a.reshape(-1, 4)[:, :3].max() > 0.002:
            exp["emissive"] = em

    # --- material de export + GLB --------------------------------------
    mat_pintura = bpy.data.materials.get(f"{nome}_PINTURA")
    mat_exp = construir_material(f"{nome}_EXPORT", exp, empacotado=True)
    obj.data.materials.clear()
    obj.data.materials.append(mat_exp)
    caminho = str(pasta.parent / f"{nome}.glb")
    selecionar([obj])
    try:
        bpy.ops.export_scene.gltf(filepath=caminho, export_format='GLB',
                                  use_selection=True, export_apply=True,
                                  export_image_format='AUTO')
    finally:
        obj.data.materials.clear()
        if mat_pintura:
            obj.data.materials.append(mat_pintura)
        bpy.data.materials.remove(mat_exp)

    avisos = []
    if triangulos(obj) > 20000:
        avisos.append("mais de 20k triângulos (raylib usa índices de 16 bits)")
    if any(abs(s - 1.0) > 1e-4 for s in obj.scale):
        avisos.append("escala não aplicada")
    if len(obj.data.uv_layers) == 0:
        avisos.append("sem UV")
    return caminho, avisos


# ------------------------------ interface --------------------------------
def _objeto_malha(op, context):
    obj = context.active_object
    if not obj or obj.type != 'MESH':
        op.report({'ERROR'}, "Selecione a malha primeiro.")
        return None
    return obj


class PBR_OT_preparar(bpy.types.Operator):
    bl_idname = "pbr.preparar"
    bl_label = "1) Preparar + bake"
    bl_description = "Low-poly, UV, texturas PBR, material e bake; entra no modo de pintura"

    def execute(self, context):
        obj = _objeto_malha(self, context)
        if not obj:
            return {'CANCELLED'}
        try:
            low = preparar(obj)
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, f"Falhou: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Pronto: {low.name} ({triangulos(low)} tris). Pode pintar.")
        return {'FINISHED'}


class PBR_OT_pintar(bpy.types.Operator):
    bl_idname = "pbr.pintar"
    bl_label = "2) Modo pintura"

    def execute(self, context):
        obj = _objeto_malha(self, context)
        if not obj:
            return {'CANCELLED'}
        nome = nome_do(obj)
        mat = bpy.data.materials.get(f"{nome}_PINTURA")
        imgs = {k: bpy.data.images.get(f"{nome}_{k}") for k in CANAIS}
        if not mat or not imgs["albedo"]:
            self.report({'ERROR'}, "Rode o passo 1 antes.")
            return {'CANCELLED'}
        entrar_modo_pintura(obj, mat, imgs)
        return {'FINISHED'}


class PBR_OT_finalizar(bpy.types.Operator):
    bl_idname = "pbr.finalizar"
    bl_label = "3) Finalizar + exportar GLB"
    bl_description = "Salva texturas, empacota ORM e exporta o GLB"

    def execute(self, context):
        obj = _objeto_malha(self, context)
        if not obj:
            return {'CANCELLED'}
        try:
            caminho, avisos = finalizar(obj)
        except Exception as e:
            traceback.print_exc()
            self.report({'ERROR'}, f"Falhou: {e}")
            return {'CANCELLED'}
        self.report({'INFO'}, f"GLB exportado: {caminho}")
        for a in avisos:
            self.report({'WARNING'}, a)
        return {'FINISHED'}


class PBR_PT_painel(bpy.types.Panel):
    bl_label = "PBR Paint Prep"
    bl_idname = "PBR_PT_painel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "PBR"

    def draw(self, context):
        col = self.layout.column(align=True)
        col.operator("pbr.preparar", icon='UV')
        col.operator("pbr.pintar", icon='BRUSH_DATA')
        col.operator("pbr.finalizar", icon='EXPORT')
        col.separator()
        col.label(text="Pinte: albedo, roughness, metallic")
        col.label(text="Não pinte: normal e AO (bake)")


CLASSES = (PBR_OT_preparar, PBR_OT_pintar, PBR_OT_finalizar, PBR_PT_painel)


def register():
    for c in CLASSES:
        antigo = getattr(bpy.types, c.__name__, None)
        if antigo:
            try:
                bpy.utils.unregister_class(antigo)
            except Exception:
                pass
        bpy.utils.register_class(c)
    print("PBR Paint Prep carregado: aba 'PBR' na barra lateral do viewport (N).")


register()
