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
