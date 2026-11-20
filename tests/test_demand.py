from app.services.demand import classify


def test_demand_patterns():
    assert classify("有没有批量处理 Excel 的工具？") == "工具需求"
    assert classify("这个软件能不能支持 Mac？") == "功能需求"
    assert classify("文件太多了应该怎么办？") == "问题求解"
    assert classify("求一个免费的替代品") == "替代需求"


def test_noise_is_filtered():
    assert classify("哈哈哈哈") is None
    assert classify("顶") is None
