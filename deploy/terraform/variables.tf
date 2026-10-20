variable "project_id" {
  type        = string
  description = "Target project; no deployment project is implicit."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "provider_secret_names" {
  type        = set(string)
  default     = []
  description = "Existing provider secret names, never secret values. Omission does not discover secrets."
  validation {
    condition = alltrue([for name in var.provider_secret_names : contains([
      "groundwork-openai-api-key", "groundwork-anthropic-api-key",
      "groundwork-google-api-key", "groundwork-openrouter-api-key"
    ], name)])
    error_message = "Only Groundwork provider secrets can be managed."
  }
}
