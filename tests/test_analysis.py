from app.services.analysis import _clusters, _tokens


def test_local_semantic_tokens_share_chinese_bigrams():
    assert _tokens("有没有自动整理文件的软件") & _tokens("自动整理文件工具")


def test_clustering_groups_similar_needs():
    items = [
        {"category": "工具需求", "semantic_text": "自动整理电脑文件和目录"},
        {"category": "工具需求", "semantic_text": "电脑文件自动分类整理"},
        {"category": "问题求解", "semantic_text": "怎么做红烧肉"},
    ]
    groups = _clusters(items, None)
    assert sorted(len(group) for group in groups) == [1, 2]
