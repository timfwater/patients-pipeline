# 🧠 Agentic AI Risk Pipeline

This project uses OpenAI's GPT model to analyze unstructured patient notes and identify high-risk patients needing follow-up care. The system automatically:

1. Scores risk using LLM prompts.
2. Recommends follow-up or specialist care.
3. Emails a summary to stakeholders.
4. Saves results + audit metadata to S3.

---

## 🚀 Deployment Options

| Method   | Status     |
|----------|------------|
| EC2 + Docker | ✅ Production-ready |
| AWS Fargate (ECS) | ✅ Fully automated |

---

## 📁 Input Format

`augmented_input.csv` (S3)

Required columns:
- `idx`: unique identifier
- `visit_date`: datetime string (e.g. `2024-03-01`)
- `full_note`: free-text provider note
- `physician_id`: int

---

## 🧠 Model Logic

| Prompt | Output |
|--------|--------|
| `RISK_PROMPT` | `risk_rating`, `risk_score` |
| `COMBINED_PROMPT` (top N%) | follow-up + specialist recommendations |

---

## 📧 Email Example

Summary email includes:
- Patient ID, risk score
- Follow-up: 1 month / 6 months
- Top 5 medical concerns
- Oncology/Cardiology flags

---

## ✅ Output

- `output.csv` → full merged result
- `audit_logs/*.json` → ECS metadata, runtime, filters, high-risk count, email flag

---

## 🔐 Secrets & Env Vars

Stored via AWS:
- OpenAI API Key → Secrets Manager: `openai/api-key`

Passed via `.env` or task definition:

| Name | Purpose |
|------|---------|
| `INPUT_S3` | `s3://.../augmented_input.csv` |
| `OUTPUT_S3` | `s3://.../output.csv` |
| `EMAIL_TO` / `EMAIL_FROM` | SES email routing |
| `PHYSICIAN_ID_LIST` | Optional comma-separated filter |
| `START_DATE` / `END_DATE` | Optional YYYY-MM-DD filters |

---

## 🛠️ Fargate Deployment

Sample CLI launch:

```bash
aws ecs run-task \
  --cluster patient-pipeline-cluster \
  --launch-type FARGATE \
  --network-configuration ... \
  --task-definition patient-pipeline-task:XX \
  --overrides file://env-overrides.json
