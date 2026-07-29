# ProfAgent

支持型私人穿搭师的数据仓库，包含简单 Schema、合成衣橱、合成商品和固定评测集。

## 首批规模

- 3 个虚拟用户
- 50 件合成衣橱单品
- 20 套穿搭
- 50 条中文电商风格 Mock 商品
- 30 条评测样本

## 数据声明

本仓库首批数据全部为合成或人工设计数据，不包含真实用户信息，不抓取或复制淘宝、京东等购物平台的真实商品、品牌、商家、图片、评论或页面内容。Mock 商品仅借鉴中文购物平台常见的信息组织与描述方式。

## 文件

```text
data/
  schemas/       # user / garment / outfit / catalog / eval
  fixtures/      # users / garments / outfits / catalog
  eval/          # 固定评测数据
  manifests/     # 数据计数和哈希
  sources.yaml   # 数据来源与使用边界
  vocab.yaml     # 简单受控标签
  version.yaml   # 数据版本
scripts/
  generate.py
  validate.py
tests/
```

## 校验

```powershell
python -m pip install -e ".[dev]"
python scripts/validate.py
python -m pytest -q
```

## 核心规则

- `persona_id` 固定为 `stylist`。
- `now`、`today`、`unknown` 默认是高急切度，禁止商品检索。
- 穿搭中的衣物 ID 必须存在，并属于同一个用户。
- 禁忌色、无库存、不适合季节和不可用衣物在召回前过滤。
