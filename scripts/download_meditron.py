from huggingface_hub import snapshot_download

# Download all files from the model repo to the specified local directory
snapshot_download(
    repo_id="OpenMeditron/Meditron3-Qwen2.5-7B",
    local_dir="hugging_face",
    local_dir_use_symlinks=False
)