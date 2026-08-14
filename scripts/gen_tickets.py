"""生成模拟工单脚本：python scripts/gen_tickets.py

按真实客服场景的分布比例生成一批带「真实标签」的工单，
用于跑通流程、评测分类准确率与自动处理率。
生成结果保存到 data/sample_tickets.json。
"""
import json
import random
import sys
from pathlib import Path

# 把项目根目录加入模块搜索路径，方便直接 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config, setup_logging

# 每个分类的工单样例（带多种口语化表达，更贴近真实场景）
SAMPLES = {
    "物流查询": [
        "我的快递三天没更新物流了，帮我查查到哪了",
        "包裹显示已发货，但一直没动，怎么回事",
        "物流信息停在转运中心两天了，正常吗",
        "帮我查一下这个包裹现在到哪个城市了",
        "快递显示派送中，但一直没收到，能催一下吗",
        "我买的东西到哪了，麻烦查一下物流",
        "包裹显示签收了，但我没收到，什么情况",
        "物流更新太慢了，能不能帮我催催快递",
    ],
    "退货申请": [
        "衣服尺码不合适，想退货，怎么操作",
        "鞋子穿着不舒服，想申请退货，流程是什么",
        "收到的东西和图片不符，我要退货",
        "不想要了，还没拆封，可以退货吗",
        "帮我申请一下退货，寄到哪里",
        "退货地址在哪里，怎么填退货单",
        "质量太差了，我要退货退款",
        "七天无理由退货，我这个还在期限内吗",
    ],
    "商品咨询": [
        "你们家空气炸锅的保修期是多久",
        "产品使用说明书在哪里看",
        "这个电饭煲怎么预约煮饭",
        "支持哪些支付方式",
        "会员积分有什么用",
        "优惠券怎么用",
        "商品支持七天无理由吗",
        "这款产品的参数能发我看看吗",
    ],
    "售后维修": [
        "机器坏了，需要工程师上门检修",
        "用了两次就坏了，怎么报修",
        "维修流程是怎样的，寄到哪里修",
        "屏幕碎了能修吗，多少钱",
        "保修期内坏了免费修吗",
        "怎么申请售后维修",
        "维修要多久能修好",
        "需要提供什么凭证才能维修",
    ],
    "发票问题": [
        "订单完成后怎么开电子发票",
        "发票抬头填错了能改吗",
        "发票一般多久能开出来",
        "电子发票在哪里下载",
        "能开增值税专用发票吗",
        "发票信息填错了怎么办",
        "为什么我的订单开不了发票",
    ],
    "退款纠纷": [
        "商家拒绝退款，多次沟通无果",
        "退款金额少了十块钱，怎么回事",
        "说好的退款一直不到账，商家不处理",
        "我申请退款被拒绝了，理由不充分",
        "退款拖了半个月还不处理",
        "商家只肯退一半，我不接受",
        "平台客服介入也没用，还是不给退",
    ],
    "人工处理工单": [
        "商品损坏要求赔偿500元，不接受标准售后方案",
        "我要退款还要赔偿我的损失",
        "这个产品害我损失了很多钱，必须给个说法",
        "同时要求退货和补偿，你们看着办",
        "太气人了，我要投诉还要赔偿",
        "情况比较复杂，你们直接派专人和我谈",
    ],
}

# 每个分类的出现次数权重（决定模拟工单的分布比例）
# 自动处理类（物流/退货/商品咨询/售后维修/发票）约占 64%，转人工类约占 36%
WEIGHTS = {
    "物流查询": 15,
    "退货申请": 13,
    "商品咨询": 12,
    "售后维修": 12,
    "发票问题": 12,
    "退款纠纷": 20,
    "人工处理工单": 16,
}


def generate(count: int = 100, seed: int = 42) -> list[dict]:
    """生成 count 张带真实标签的模拟工单。"""
    random.seed(seed)
    tickets = []

    categories = list(WEIGHTS.keys())
    weights = list(WEIGHTS.values())
    for _ in range(count):
        category = random.choices(categories, weights=weights, k=1)[0]
        text = random.choice(SAMPLES[category])
        tickets.append({"ticket_text": text, "ground_truth": category})

    return tickets


if __name__ == "__main__":
    setup_logging()
    tickets = generate(100)
    output = Config.DATA_DIR / "sample_tickets.json"
    output.write_text(
        json.dumps(tickets, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[完成] 已生成 {len(tickets)} 张模拟工单，保存到 {output}")
