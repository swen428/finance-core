#include <node_api.h>

#include <cerrno>
#include <climits>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <string>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>
#include <vector>

#if defined(__linux__)
#include <linux/fs.h>
#include <sys/syscall.h>
#endif

namespace {

void ThrowErrno(napi_env env, const char* operation) {
  const int error_number = errno;
  std::string message(operation);
  message.append(": ");
  message.append(std::strerror(error_number));
  napi_value message_value;
  napi_create_string_utf8(env, message.c_str(), message.size(), &message_value);
  napi_value error;
  napi_create_error(env, nullptr, message_value, &error);
  const char* code_name = "EIO";
  switch (error_number) {
    case EACCES: code_name = "EACCES"; break;
    case EEXIST: code_name = "EEXIST"; break;
    case EINVAL: code_name = "EINVAL"; break;
    case ELOOP: code_name = "ELOOP"; break;
    case ENOENT: code_name = "ENOENT"; break;
    case ENOSPC: code_name = "ENOSPC"; break;
    case ENOTDIR: code_name = "ENOTDIR"; break;
    case EPERM: code_name = "EPERM"; break;
    default: break;
  }
  napi_value code;
  napi_create_string_utf8(env, code_name, NAPI_AUTO_LENGTH, &code);
  napi_set_named_property(env, error, "code", code);
  napi_throw(env, error);
}

bool GetInt32(napi_env env, napi_value value, int32_t* output) {
  if (napi_get_value_int32(env, value, output) != napi_ok) {
    napi_throw_type_error(env, nullptr, "Expected an integer file descriptor or flag value.");
    return false;
  }
  return true;
}

bool GetString(napi_env env, napi_value value, std::string* output) {
  size_t length = 0;
  if (napi_get_value_string_utf8(env, value, nullptr, 0, &length) != napi_ok ||
      length == 0 || length > NAME_MAX) {
    napi_throw_type_error(env, nullptr, "Expected a bounded non-empty UTF-8 path component.");
    return false;
  }
  std::vector<char> buffer(length + 1);
  if (napi_get_value_string_utf8(env, value, buffer.data(), buffer.size(), &length) != napi_ok) {
    napi_throw_type_error(env, nullptr, "Expected a UTF-8 string.");
    return false;
  }
  output->assign(buffer.data(), length);
  if (*output == "." || *output == ".." || output->find('/') != std::string::npos ||
      output->find('\0') != std::string::npos) {
    napi_throw_type_error(env, nullptr, "Expected one safe directory-entry name.");
    return false;
  }
  return true;
}

bool GetPath(napi_env env, napi_value value, std::string* output) {
  size_t length = 0;
  if (napi_get_value_string_utf8(env, value, nullptr, 0, &length) != napi_ok ||
      length == 0 || length > PATH_MAX) {
    napi_throw_type_error(env, nullptr, "Expected a bounded non-empty absolute path.");
    return false;
  }
  std::vector<char> buffer(length + 1);
  if (napi_get_value_string_utf8(env, value, buffer.data(), buffer.size(), &length) != napi_ok) {
    napi_throw_type_error(env, nullptr, "Expected a UTF-8 path.");
    return false;
  }
  output->assign(buffer.data(), length);
  if (output->front() != '/' || output->find('\0') != std::string::npos) {
    napi_throw_type_error(env, nullptr, "Expected an absolute path without NUL bytes.");
    return false;
  }
  return true;
}

napi_value IntResult(napi_env env, int value) {
  napi_value result;
  napi_create_int32(env, value, &result);
  return result;
}

napi_value DescriptorIdentityResult(napi_env env, const struct stat& status) {
  napi_value result;
  napi_create_object(env, &result);

  napi_value dev;
  napi_create_bigint_uint64(env, static_cast<uint64_t>(status.st_dev), &dev);
  napi_set_named_property(env, result, "dev", dev);
  napi_value ino;
  napi_create_bigint_uint64(env, static_cast<uint64_t>(status.st_ino), &ino);
  napi_set_named_property(env, result, "ino", ino);
  napi_value uid;
  napi_create_uint32(env, static_cast<uint32_t>(status.st_uid), &uid);
  napi_set_named_property(env, result, "uid", uid);
  napi_value mode;
  napi_create_uint32(env, static_cast<uint32_t>(status.st_mode), &mode);
  napi_set_named_property(env, result, "mode", mode);
  napi_value size;
  napi_create_double(env, static_cast<double>(status.st_size), &size);
  napi_set_named_property(env, result, "size", size);
#if defined(__APPLE__)
  const struct timespec ctime = status.st_ctimespec;
  const struct timespec mtime = status.st_mtimespec;
#else
  const struct timespec ctime = status.st_ctim;
  const struct timespec mtime = status.st_mtim;
#endif
  napi_value ctime_ns;
  napi_create_bigint_int64(
      env,
      static_cast<int64_t>(ctime.tv_sec) * 1000000000LL + ctime.tv_nsec,
      &ctime_ns);
  napi_set_named_property(env, result, "ctimeNs", ctime_ns);
  napi_value mtime_ns;
  napi_create_bigint_int64(
      env,
      static_cast<int64_t>(mtime.tv_sec) * 1000000000LL + mtime.tv_nsec,
      &mtime_ns);
  napi_set_named_property(env, result, "mtimeNs", mtime_ns);
  napi_value is_directory;
  napi_get_boolean(env, S_ISDIR(status.st_mode), &is_directory);
  napi_set_named_property(env, result, "isDirectory", is_directory);
  napi_value is_file;
  napi_get_boolean(env, S_ISREG(status.st_mode), &is_file);
  napi_set_named_property(env, result, "isFile", is_file);
  return result;
}

napi_value Undefined(napi_env env) {
  napi_value result;
  napi_get_undefined(env, &result);
  return result;
}

napi_value OpenDirectory(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value args[1];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  std::string path;
  if (argc != 1 || !GetPath(env, args[0], &path)) return nullptr;
  const int fd = open(path.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) { ThrowErrno(env, "open directory"); return nullptr; }
  return IntResult(env, fd);
}

napi_value OpenDirectoryAt(napi_env env, napi_callback_info info) {
  size_t argc = 3;
  napi_value args[3];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t parent_fd;
  int32_t mode;
  std::string name;
  if (argc != 3 || !GetInt32(env, args[0], &parent_fd) || !GetString(env, args[1], &name) ||
      !GetInt32(env, args[2], &mode)) return nullptr;
  if (mkdirat(parent_fd, name.c_str(), static_cast<mode_t>(mode)) < 0 && errno != EEXIST) {
    ThrowErrno(env, "mkdirat");
    return nullptr;
  }
  const int fd = openat(parent_fd, name.c_str(),
                        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) { ThrowErrno(env, "openat directory"); return nullptr; }
  return IntResult(env, fd);
}

napi_value OpenExistingDirectoryAt(napi_env env, napi_callback_info info) {
  size_t argc = 2;
  napi_value args[2];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t parent_fd;
  std::string name;
  if (argc != 2 || !GetInt32(env, args[0], &parent_fd) || !GetString(env, args[1], &name)) {
    return nullptr;
  }
  const int fd = openat(parent_fd, name.c_str(),
                        O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (fd < 0) { ThrowErrno(env, "openat existing directory"); return nullptr; }
  return IntResult(env, fd);
}

napi_value OpenFileAt(napi_env env, napi_callback_info info) {
  size_t argc = 4;
  napi_value args[4];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t directory_fd;
  int32_t flags;
  int32_t mode;
  std::string name;
  if (argc != 4 || !GetInt32(env, args[0], &directory_fd) || !GetString(env, args[1], &name) ||
      !GetInt32(env, args[2], &flags) || !GetInt32(env, args[3], &mode)) return nullptr;
  const int fd = openat(directory_fd, name.c_str(), flags | O_NOFOLLOW | O_CLOEXEC,
                        static_cast<mode_t>(mode));
  if (fd < 0) { ThrowErrno(env, "openat file"); return nullptr; }
  return IntResult(env, fd);
}

napi_value DescriptorIdentitySync(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value args[1];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t fd;
  if (argc != 1 || !GetInt32(env, args[0], &fd)) return nullptr;
  struct stat status {};
  if (fstat(fd, &status) < 0) {
    ThrowErrno(env, "fstat descriptor");
    return nullptr;
  }
  return DescriptorIdentityResult(env, status);
}

napi_value RenameNoReplaceAt(napi_env env, napi_callback_info info) {
  size_t argc = 3;
  napi_value args[3];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t directory_fd;
  std::string source;
  std::string target;
  if (argc != 3 || !GetInt32(env, args[0], &directory_fd) ||
      !GetString(env, args[1], &source) || !GetString(env, args[2], &target)) return nullptr;
#if defined(__APPLE__)
  const int result = renameatx_np(directory_fd, source.c_str(), directory_fd, target.c_str(),
                                  RENAME_EXCL);
#elif defined(__linux__)
  const int result = static_cast<int>(syscall(SYS_renameat2, directory_fd, source.c_str(),
                                              directory_fd, target.c_str(), RENAME_NOREPLACE));
#else
#error "finance-bridge requires atomic no-replace rename support"
#endif
  if (result < 0) {
    ThrowErrno(env, "atomic no-replace rename");
    return nullptr;
  }
  return Undefined(env);
}

napi_value ListAt(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value args[1];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t directory_fd;
  if (argc != 1 || !GetInt32(env, args[0], &directory_fd)) return nullptr;
  const int duplicate = openat(directory_fd, ".", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
  if (duplicate < 0) { ThrowErrno(env, "dup directory"); return nullptr; }
  DIR* directory = fdopendir(duplicate);
  if (directory == nullptr) {
    close(duplicate);
    ThrowErrno(env, "fdopendir");
    return nullptr;
  }
  napi_value result;
  napi_create_array(env, &result);
  uint32_t index = 0;
  errno = 0;
  while (dirent* entry = readdir(directory)) {
    if (std::strcmp(entry->d_name, ".") == 0 || std::strcmp(entry->d_name, "..") == 0) continue;
    napi_value name;
    napi_create_string_utf8(env, entry->d_name, NAPI_AUTO_LENGTH, &name);
    napi_set_element(env, result, index++, name);
  }
  const int read_error = errno;
  closedir(directory);
  if (read_error != 0) {
    errno = read_error;
    ThrowErrno(env, "readdir");
    return nullptr;
  }
  return result;
}

napi_value FreeBytes(napi_env env, napi_callback_info info) {
  size_t argc = 1;
  napi_value args[1];
  napi_get_cb_info(env, info, &argc, args, nullptr, nullptr);
  int32_t directory_fd;
  if (argc != 1 || !GetInt32(env, args[0], &directory_fd)) return nullptr;
  struct statvfs status {};
  if (fstatvfs(directory_fd, &status) < 0) {
    ThrowErrno(env, "fstatvfs");
    return nullptr;
  }
  const double bytes = static_cast<double>(status.f_bavail) *
                       static_cast<double>(status.f_frsize == 0 ? status.f_bsize : status.f_frsize);
  napi_value result;
  napi_create_double(env, bytes, &result);
  return result;
}

napi_value Initialize(napi_env env, napi_value exports) {
  const napi_property_descriptor properties[] = {
      {"openDirectory", nullptr, OpenDirectory, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"openDirectoryAt", nullptr, OpenDirectoryAt, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"openExistingDirectoryAt", nullptr, OpenExistingDirectoryAt, nullptr, nullptr, nullptr,
       napi_default, nullptr},
      {"openFileAt", nullptr, OpenFileAt, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"descriptorIdentitySync", nullptr, DescriptorIdentitySync, nullptr, nullptr, nullptr,
       napi_default, nullptr},
      {"renameNoReplaceAt", nullptr, RenameNoReplaceAt, nullptr, nullptr, nullptr, napi_default,
       nullptr},
      {"listAt", nullptr, ListAt, nullptr, nullptr, nullptr, napi_default, nullptr},
      {"freeBytes", nullptr, FreeBytes, nullptr, nullptr, nullptr, napi_default, nullptr},
  };
  napi_define_properties(env, exports, sizeof(properties) / sizeof(properties[0]), properties);
  return exports;
}

}  // namespace

NAPI_MODULE(NODE_GYP_MODULE_NAME, Initialize)
