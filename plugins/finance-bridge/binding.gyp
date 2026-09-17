{
  "targets": [
    {
      "target_name": "finance_bridge_posix",
      "sources": ["native/finance_bridge_posix.cc"],
      "cflags_cc": ["-std=c++17"],
      "xcode_settings": {
        "CLANG_CXX_LANGUAGE_STANDARD": "c++17",
        "GCC_ENABLE_CPP_EXCEPTIONS": "NO"
      }
    }
  ]
}
