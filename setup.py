import os
import re
import subprocess
import sys
from pathlib import Path

from setuptools import Extension, find_packages, setup
from setuptools.command.build_ext import build_ext


# Read the configuration settings
class UserInputRequired(Exception):
    """
    Defines a custom exception that signals the need for user input to proceed
    with a particular operation or action. It serves as a signal to handle missing
    or invalid user data, ensuring program flow continues after receiving required
    input from users.

    """
    def __init__(self, msg):
        super().__init__(msg)


FRI_CLIENT_VERSION = os.environ.get("FRI_CLIENT_VERSION")
if FRI_CLIENT_VERSION is None:
    raise UserInputRequired(
        "Please set the environment variable FRI_CLIENT_VERSION to the version of the FRI Client SDK you are using."
    )

# Convert distutils Windows platform specifiers to CMake -A arguments
PLAT_TO_CMAKE = {
    "win32": "Win32",
    "win-amd64": "x64",
    "win-arm32": "ARM",
    "win-arm64": "ARM64",
}


# A CMakeExtension needs a sourcedir instead of a file list.
# The name must be the _single_ output extension from the CMake build.
# If you need multiple extensions, see scikit-build.
class CMakeExtension(Extension):
    """
    Initializes a CMake extension project with given name and source directory
    path. It sets up the basic structure for a CMake build, allowing sources to
    be added later. The `sourcedir` parameter is resolved to an absolute file
    system path.

    Attributes:
        sourcedir (osPathLike[str]|str): Set to a normalized path string. It stores
            the source directory for the extension. This value can be obtained
            from a Path object, which provides file system operations.

    """
    def __init__(self, name: str, sourcedir: str = "") -> None:
        """
        Initializes an instance with a specified name and optionally a sourcedir
        path. The `super().__init__` call creates a base Extension instance with
        the given name, while also initializing a sources list. The sourcedir path
        is resolved and stored in self.sourcedir.

        Args:
            name (str): Required for initialization. It represents the name of an
                object being created and will be used as its identifier.
            sourcedir (str): Optional, as indicated by its default value. It
                represents the source directory path that can be resolved to an
                absolute path using os.fspath and Path.resolve.

        """
        super().__init__(name, sources=[])
        self.sourcedir = os.fspath(Path(sourcedir).resolve())


class CMakeBuild(build_ext):
    """
    Extends the `build_ext` class from Python's `setuptools` package to provide
    custom CMake-based build logic for extension modules. It generates and builds
    CMake project files, incorporating environment variables and project-specific
    settings.

    """
    def build_extension(self, ext: CMakeExtension) -> None:
        """
        Builds and configures a CMake-based extension using specified environment
        variables, compiler types, and configuration settings to generate native
        libraries for various platforms and architectures.

        Args:
            ext (CMakeExtension): Expected to be a specific extension that needs
                to be built.

        """
        # Must be in this form due to bug in .resolve() only fixed in Python 3.10+
        ext_fullpath = Path.cwd() / self.get_ext_fullpath(ext.name)
        extdir = ext_fullpath.parent.resolve()

        # Using this requires trailing slash for auto-detection & inclusion of
        # auxiliary "native" libs
        debug = int(os.environ.get("DEBUG", 0)) if self.debug is None else self.debug
        cfg = "Debug" if debug else "Release"

        # CMake lets you override the generator - we need to check this.
        # Can be set with Conda-Build, for example.
        cmake_generator = os.environ.get("CMAKE_GENERATOR", "")

        # Set Python_EXECUTABLE instead if you use PYBIND11_FINDPYTHON
        # EXAMPLE_VERSION_INFO shows you how to pass a value into the C++ code
        # from Python.
        cmake_args = [
            f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY={extdir}{os.sep}",
            f"-DPYTHON_EXECUTABLE={sys.executable}",
            f"-DCMAKE_BUILD_TYPE={cfg}",  # not used on MSVC, but no harm
        ]
        build_args = []
        # Adding CMake arguments set as environment variable
        # (needed e.g. to build for ARM OSx on conda-forge)
        if "CMAKE_ARGS" in os.environ:
            cmake_args += [item for item in os.environ["CMAKE_ARGS"].split(" ") if item]

        # In this example, we pass in the version to C++. You might not need to.
        # cmake_args += [f"-DEXAMPLE_VERSION_INFO={self.distribution.get_version()}"]

        if self.compiler.compiler_type != "msvc":
            # Using Ninja-build since it a) is available as a wheel and b)
            # multithreads automatically. MSVC would require all variables be
            # exported for Ninja to pick it up, which is a little tricky to do.
            # Users can override the generator with CMAKE_GENERATOR in CMake
            # 3.15+.
            if not cmake_generator or cmake_generator == "Ninja":
                try:
                    import ninja

                    ninja_executable_path = Path(ninja.BIN_DIR) / "ninja"
                    cmake_args += [
                        "-GNinja",
                        f"-DCMAKE_MAKE_PROGRAM:FILEPATH={ninja_executable_path}",
                    ]
                except ImportError:
                    pass

        else:
            # Single config generators are handled "normally"
            single_config = any(x in cmake_generator for x in {"NMake", "Ninja"})

            # CMake allows an arch-in-generator style for backward compatibility
            contains_arch = any(x in cmake_generator for x in {"ARM", "Win64"})

            # Specify the arch if using MSVC generator, but only if it doesn't
            # contain a backward-compatibility arch spec already in the
            # generator name.
            if not single_config and not contains_arch:
                cmake_args += ["-A", PLAT_TO_CMAKE[self.plat_name]]

            # Multi-config generators have a different way to specify configs
            if not single_config:
                cmake_args += [
                    f"-DCMAKE_LIBRARY_OUTPUT_DIRECTORY_{cfg.upper()}={extdir}"
                ]
                build_args += ["--config", cfg]

        if sys.platform.startswith("darwin"):
            # Cross-compile support for macOS - respect ARCHFLAGS if set
            archs = re.findall(r"-arch (\S+)", os.environ.get("ARCHFLAGS", ""))
            if archs:
                cmake_args += ["-DCMAKE_OSX_ARCHITECTURES={}".format(";".join(archs))]

        # Set CMAKE_BUILD_PARALLEL_LEVEL to control the parallel build level
        # across all generators.
        if "CMAKE_BUILD_PARALLEL_LEVEL" not in os.environ:
            # self.parallel is a Python 3 only way to set parallel jobs by hand
            # using -j in the build_ext call, not supported by pip or PyPA-build.
            if hasattr(self, "parallel") and self.parallel:
                # CMake 3.12+ only.
                build_args += [f"-j{self.parallel}"]

        # Set the FRI version number
        fri_ver_major = FRI_CLIENT_VERSION.split(".")[0]
        fri_ver_minor = FRI_CLIENT_VERSION.split(".")[1]
        cmake_args += [f"-DFRI_CLIENT_VERSION_MAJOR={fri_ver_major}"]
        cmake_args += [f"-DFRI_CLIENT_VERSION_MINOR={fri_ver_minor}"]

        build_temp = Path(self.build_temp) / ext.name
        if not build_temp.exists():
            build_temp.mkdir(parents=True)

        subprocess.run(
            ["cmake", ext.sourcedir, *cmake_args], cwd=build_temp, check=True
        )
        subprocess.run(
            ["cmake", "--build", ".", *build_args], cwd=build_temp, check=True
        )


setup(
    name="pyfri",
    version="1.2.1",
    author="Christopher E. Mower, Martin Huber",
    author_email="christopher.mower@kcl.ac.uk, m.huber_1994@hotmail.de",
    description="Python bindings for the FRI Client SDK library.",
    long_description="",
    packages=find_packages(),
    ext_modules=[CMakeExtension("_pyfri")],
    install_requires=["numpy", "pygame", "pyoptas", "pandas", "matplotlib"],
    cmdclass={"build_ext": CMakeBuild},
    python_requires=">=3.8",
)
