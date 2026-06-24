from music_assembler.api.uploader_client import merge_channel_list


def test_merge_channel_list_uploader_first_with_r2_extras():
    uploader = [
        {"id": "nappabeats", "name": "NappaBeats", "source": "uploader"},
        {"id": "sapporobeats", "name": "Sapporo Beats", "source": "uploader"},
    ]
    ids, details = merge_channel_list(
        uploader_channels=uploader,
        configured=("legacy-slug",),
        discovered=["legacy-slug", "orphan-r2"],
    )
    assert ids == ["legacy-slug", "nappabeats", "orphan-r2", "sapporobeats"]
    assert {d["id"] for d in details} == {"nappabeats", "sapporobeats", "legacy-slug", "orphan-r2"}
    by_id = {d["id"]: d for d in details}
    assert by_id["nappabeats"]["name"] == "NappaBeats"
    assert by_id["orphan-r2"]["source"] == "r2"
