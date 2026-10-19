// Readers still build the checked-out source; they only skip cache uploads.
variable "CI_CACHE_WRITE" {
  default = "false"
}

group "runtime" {
  targets = ["viewer", "agent"]
}

group "platform" {
  targets = ["control", "science"]
}

target "viewer" {
  context    = "."
  dockerfile = "Dockerfile"
  tags       = ["a2a-comm-local:latest"]
  cache-from = ["type=gha,scope=viewer"]
  cache-to   = CI_CACHE_WRITE == "true" ? ["type=gha,mode=max,scope=viewer"] : []
}

target "agent" {
  context    = "."
  dockerfile = "a2a-agent/runtime/Dockerfile"
  tags       = ["a2a-agent-runtime:local"]
  cache-from = ["type=gha,scope=agent"]
  cache-to   = CI_CACHE_WRITE == "true" ? ["type=gha,mode=max,scope=agent"] : []
}

target "control" {
  context    = "."
  dockerfile = "Dockerfile.platform"
  tags       = ["groundwork-platform:local"]
  cache-from = ["type=gha,scope=platform"]
  cache-to   = CI_CACHE_WRITE == "true" ? ["type=gha,mode=max,scope=platform"] : []
}

target "science" {
  context    = "."
  dockerfile = "Dockerfile.runtime"
  tags       = ["groundwork-runtime:local"]
  cache-from = ["type=gha,scope=science"]
  cache-to   = CI_CACHE_WRITE == "true" ? ["type=gha,mode=max,scope=science"] : []
}
