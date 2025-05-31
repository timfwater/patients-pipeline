#!/bin/bash
# 📦 EC2 User Data Script (runs at boot)

# System prep
yum update -y
amazon-linux-extras install docker -y
service docker start
usermod -a -G docker ec2-user

# Pull latest repo (adjust URL if private repo or SSH required)
cd /home/ec2-user
git clone https://github.com/timfwater/patient-pipeline.git
chown -R ec2-user:ec2-user patient-pipeline

# Optional: Build and run Docker (uncomment to auto-launch)
# cd patient-pipeline
# docker build -t patient-pipeline .
# docker run --env-file ec2_deployment/env.list patient-pipeline

# Log for verification
echo "✅ EC2 setup complete" >> /var/log/user-data.log
