# Silkroad Asset Builder

Ferramenta simples em Python para tarefas comuns com assets do Silkroad Online.

## Recursos

- extrair um ou vários pacotes `.pk2`;
- converter PNG, JPG, BMP e DDS para `.ddj`;
- transformar OBJ + textura em uma malha de efeito (`.bms` + `.ddj`);
- montar um pacote customizado `.bsr/.bmt/.bms/.ddj`.

O extrator PK2 copia os arquivos em fluxo e atualiza somente o progresso. Ele não
carrega pacotes inteiros na memória e não abre o Explorer durante o trabalho.

## Instalação

Requer Python 3.10 ou superior.

```powershell
python -m pip install -r requirements.txt
```

## Uso

Abra `Abrir Silkroad Asset Builder.cmd` ou execute:

```powershell
python .\sro_asset_builder.py
```

### Extrair PK2

1. Abra a aba **EXTRAIR PK2**.
2. Clique em **Selecionar PK2** e escolha um ou vários pacotes.
3. Escolha a pasta de destino.
4. Mantenha **Criar uma pasta para cada PK2** marcado para gerar pastas como
   `Data`, `Media` e `Particles` sem misturar os arquivos.
5. Clique em **EXTRAIR PACOTES**.

O leitor usa a chave padrão dos clientes Silkroad/vSRO. Pacotes protegidos com
uma chave personalizada não são suportados por enquanto.

### Texturas DDJ

Na aba **TEXTURAS DDJ**, selecione arquivos individuais ou uma pasta inteira.
O destino é opcional; se ficar vazio, os `.ddj` são salvos ao lado dos originais.

### Efeito / Prop

A aba **EFEITO / PROP** recebe:

- um modelo Wavefront OBJ;
- uma textura PNG, DDS, JPG ou BMP;
- a raiz extraída que contém `Data`, `Media` ou `Particles`.

`PROP SKILL` grava a malha em `Particles/meshes` e a textura em
`Particles/textures`. `CUSTOM PROP` cria o conjunto completo em
`Data/res/custom/<nome>`.

## Observação

Faça backup antes de substituir arquivos de um cliente. A ferramenta apenas lê
os pacotes PK2 durante a extração; ela não altera o arquivo original.
