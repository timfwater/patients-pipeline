import boto3
import sagemaker
from sagemaker.huggingface import HuggingFaceModel
import time

# --- CONFIG ---
aws_region = "us-east-1"  # Change if needed
role = "arn:aws:iam::665277163763:role/PatientPipelineECSTaskRole"  # Update if your SageMaker role is different


# Model details (Meditron3-Qwen2.5-7B)
# Download the model from Hugging Face Hub and upload to S3 as model.tar.gz
# Example: transformers-cli download mit-han-lab/meditron-3-7b-qwen2.5 --cache-dir ./model && tar -czvf model.tar.gz ./model && aws s3 cp model.tar.gz s3://your-bucket/model.tar.gz
model_data = "s3://your-bucket/model.tar.gz"  # <-- UPDATE THIS to your S3 path
instance_type = "ml.g5.xlarge"  # Reasonable for 7B models, lowest cost for GPU inference
endpoint_name = "meditron3-qwen2-5-7b-endpoint"

# Hugging Face DLC image URI (PyTorch, Transformers)
# Find the latest URI here: https://github.com/aws/deep-learning-containers/blob/master/available_images.md#huggingface-inference-containers
image_uri = (
    f"763104351884.dkr.ecr.{aws_region}.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.38.0-cpu-py310"
    if instance_type.startswith("ml.c") or instance_type.startswith("ml.m")
    else f"763104351884.dkr.ecr.{aws_region}.amazonaws.com/huggingface-pytorch-inference:2.1.0-transformers4.38.0-gpu-py310"
)

# --- Create SageMaker session ---
sess = sagemaker.Session()


# --- HuggingFace Model ---
huggingface_model = HuggingFaceModel(
    model_data=model_data,
    image_uri=image_uri,
    role=role,
    sagemaker_session=sess,
)

# --- Deploy endpoint ---
print(f"Deploying {hf_model_id} to SageMaker endpoint '{endpoint_name}' (this may take 5-10 minutes)...")
predictor = huggingface_model.deploy(
    initial_instance_count=1,
    instance_type=instance_type,
    endpoint_name=endpoint_name,
    wait=True,
)
print(f"✅ Endpoint '{endpoint_name}' is live!")

# --- Test the endpoint (optional) ---
# test_payload = {"inputs": "What is the capital of France?"}
# response = predictor.predict(test_payload)
# print("Test response:", response)

# --- Clean up (uncomment to delete endpoint when done) ---
# predictor.delete_endpoint()
# print(f"Endpoint '{endpoint_name}' deleted.")
