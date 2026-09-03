variable "aws_region" {
  description = "Approved customer sandbox Region."
  type        = string
  default     = "us-east-1"
}

variable "sandbox_bucket_name" {
  description = "Globally unique, non-sensitive name for the first sandbox bucket."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.sandbox_bucket_name))
    error_message = "Use a 3-63 character lowercase S3-compatible bucket name."
  }
}
