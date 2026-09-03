output "sandbox_bucket_name" {
  description = "Name of the customer-controlled onboarding bucket."
  value       = aws_s3_bucket.sandbox.id
}

output "sandbox_bucket_arn" {
  description = "ARN of the customer-controlled onboarding bucket."
  value       = aws_s3_bucket.sandbox.arn
}
