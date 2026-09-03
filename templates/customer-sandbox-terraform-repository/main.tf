data "aws_iam_policy_document" "sandbox_bucket_tls_only" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.sandbox.arn,
      "${aws_s3_bucket.sandbox.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket" "sandbox" {
  bucket        = var.sandbox_bucket_name
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "sandbox" {
  bucket = aws_s3_bucket.sandbox.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "sandbox" {
  bucket = aws_s3_bucket.sandbox.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "sandbox" {
  bucket = aws_s3_bucket.sandbox.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "sandbox" {
  bucket = aws_s3_bucket.sandbox.id
  policy = data.aws_iam_policy_document.sandbox_bucket_tls_only.json
}
