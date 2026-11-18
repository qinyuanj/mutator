#Requires -Version 5.1
<#
.SYNOPSIS
    Install the Lua 5.4 test toolchain on native Windows x64.

.DESCRIPTION
    mutator's Lua cases report through Busted, LuaCov and cluacov 1.0.0, and the
    mutation collector adds the LuaCov LCOV reporter. They need all of them, or
    they fail loudly under `MUTATOR_REQUIRE_TOOLCHAINS=1` instead of running.
    Before this script the job took the toolchain from leafo's third-party
    actions and a local Windows row had no documented install at all; this is
    the recipe, under the current user, with no administrator prompt:

        %USERPROFILE%\.local\lua54      Lua 5.4 interpreter, headers and lua54.lib
        %USERPROFILE%\.local\luarocks   LuaRocks 3.12.0
        %USERPROFILE%\.local\mingw64    the compiler that builds Lua and the C rocks
        %APPDATA%\luarocks              deploy tree: busted, luacov, cluacov
        %USERPROFILE%\.luarocks         luacov-reporter-lcov, user tree only

    It ends by writing PATH, LUA_PATH and LUA_CPATH, so a shell started
    afterwards runs the Lua tests instead of skipping them. In GitHub Actions
    the same three values go to `$env:GITHUB_ENV`, because a later step starts
    from the job's environment rather than this process's.

    Why a compiler at all: Lua has no official Windows binaries, and LuaRocks'
    builtin build type invokes x86_64-w64-mingw32-gcc for a C rock — luafilesystem
    and cluacov are C rocks, and Busted and LuaCov load lfs directly. The MSVC
    Build Tools cannot stand in: they fail with "Failed compiling object
    src/lfs.o", so one winlibs MinGW-w64 build covers the interpreter and both
    C rocks.

    Why the config file sits in %APPDATA%\luarocks: on Windows that directory is
    LuaRocks' deploy tree and it reads config-5.4.lua from there. A config placed
    in ~/.luarocks is silently ignored.

    Why luacov-reporter-lcov goes to the user tree: Ubuntu keeps it there,
    because Lua searches /usr/local before ./?.lua and the coverage-gate tests
    stub ./luacov/reporter/lcov.lua and require that stub to load. Windows
    repeats the split and deletes any deploy-tree copy, so both platforms have
    one shape; LUA_PATH starts with ./?.lua, so a stub is reached first anyway.

    Provenance: this is the same recipe crapper's scripts/setup-windows.ps1
    established (crapper#3, ADR 0004 there) and dsh-uml-viewer's
    scripts/setup-windows-lua.ps1 uses: the three repositories install the same
    versions and must be re-pinned together. A download hash, the Lua or
    LuaRocks version, or a rock version changed in one and not the others leaves
    the loop's three Lua suites running different toolchains.

    Re-running is safe: a component already at the pinned version is kept, the
    rocks are reinstalled over themselves, and the environment variables are not
    duplicated.

.PARAMETER LocalRoot
    Where the toolchain is unpacked. Defaults to %USERPROFILE%\.local.

.PARAMETER SkipUserEnvironment
    Leave the persistent user PATH, LUA_PATH and LUA_CPATH alone. Only those
    differ: the LuaRocks config in %APPDATA% and the rock trees are shared, so a
    throwaway LocalRoot points that config at itself until the script runs again
    with the real one.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-windows-lua.ps1
#>
[CmdletBinding()]
param(
    [string]$LocalRoot = (Join-Path $env:USERPROFILE '.local'),
    [switch]$SkipUserEnvironment
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$LuaVersion = '5.4.8'
$LuaRocksVersion = '3.12.0'
$LuaRoot = Join-Path $LocalRoot 'lua54'
$LuaRocksRoot = Join-Path $LocalRoot 'luarocks'
$MinGWRoot = Join-Path $LocalRoot 'mingw64'
$DownloadRoot = Join-Path $LocalRoot 'downloads'
$DeployTree = Join-Path $env:APPDATA 'luarocks'
$UserTree = Join-Path $env:USERPROFILE '.luarocks'
$Lua = Join-Path $LuaRoot 'bin\lua.exe'
$LuaRocks = Join-Path $LuaRocksRoot 'luarocks.exe'
$GCC = Join-Path $MinGWRoot 'bin\x86_64-w64-mingw32-gcc.exe'
$Strip = Join-Path $MinGWRoot 'bin\strip.exe'

# Pinned so this machine and the next run the same toolchain. The GitHub hashes
# are the releases' own .sha256 assets; the lua.org one is of the published
# tarball.
$Downloads = @(
    [pscustomobject]@{
        Name = "MinGW-w64 GCC 16.2.0 (winlibs)"
        File = 'winlibs-gcc-16.2.0.zip'
        Url = 'https://github.com/brechtsanders/winlibs_mingw/releases/download/16.2.0posix-14.0.0-ucrt-r2/winlibs-x86_64-posix-seh-gcc-16.2.0-mingw-w64ucrt-14.0.0-r2.zip'
        Sha256 = 'd5dbafc4a170e762ca6143151ec918fb9e2c72736fb14cd704abebc6bdd5276a'
    }
    [pscustomobject]@{
        Name = "Lua $LuaVersion source"
        File = "lua-$LuaVersion.tar.gz"
        Url = "https://www.lua.org/ftp/lua-$LuaVersion.tar.gz"
        Sha256 = '4f18ddae154e793e46eeab727c59ef1c0c0c2b744e7b94219710d76f530629ae'
    }
    [pscustomobject]@{
        Name = "LuaRocks $LuaRocksVersion"
        File = 'luarocks-3.12.0-windows-64.zip'
        Url = 'https://github.com/luarocks/luarocks/releases/download/v3.12.0/luarocks-3.12.0-windows-64.zip'
        Sha256 = '76aa3d4943e1d5204f311b232d6b6b72eb00027c54968643b5571d6ec56db57c'
    }
)

function Get-Download {
    param([pscustomobject]$Download)

    New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
    $archive = Join-Path $DownloadRoot $Download.File
    if (-not (Test-Path $archive)) {
        Write-Host "downloading $($Download.Name)"
        Invoke-WebRequest -Uri $Download.Url -OutFile $archive -UseBasicParsing
    }
    $actual = (Get-FileHash -Path $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $Download.Sha256) {
        Remove-Item $archive -Force
        throw "$($Download.Name) hashes to $actual, not $($Download.Sha256). The download was deleted; re-run to fetch it again."
    }
    return $archive
}

function New-Staging {
    $staging = Join-Path ([IO.Path]::GetTempPath()) ('lua-setup-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $staging | Out-Null
    return $staging
}

function Assert-Exit {
    param([string]$Name, [int]$ExitCode)

    if ($ExitCode -ne 0) {
        throw "$Name exited $ExitCode"
    }
}

function Test-Install {
    # Whether Exe is there and reports Pattern: a stunted or wrong-version copy is
    # not an install.
    param([string]$Exe, [string[]]$Arguments, [string]$Pattern)

    if (-not (Test-Path $Exe)) {
        return $false
    }
    try {
        $reported = (& $Exe @Arguments | Out-String)
    } catch {
        return $false
    }
    return ($LASTEXITCODE -eq 0) -and ($reported -match [regex]::Escape($Pattern))
}

# 1. The toolchain comes first: the interpreter below is compiled with it.
if (-not (Test-Install $GCC @('--version') '16.2.0')) {
    Write-Host 'installing MinGW-w64 GCC 16.2.0'
    $staging = New-Staging
    Expand-Archive -Path (Get-Download $Downloads[0]) -DestinationPath $staging -Force
    $inner = Get-ChildItem -Path $staging -Directory | Select-Object -First 1
    if (Test-Path $MinGWRoot) {
        Remove-Item $MinGWRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $MinGWRoot -Parent) | Out-Null
    Move-Item -Path $inner.FullName -Destination $MinGWRoot
    Remove-Item $staging -Recurse -Force
}

# 2. Lua itself: the DLL build Lua's own makefile mingw target makes, without
#    needing make or sh. lua.exe links the import library, so lua54.dll must sit
#    beside it.
$luaReady = (Test-Path (Join-Path $LuaRoot 'bin\lua54.dll')) -and
    (Test-Path (Join-Path $LuaRoot 'lib\lua54.lib')) -and
    (Test-Path (Join-Path $LuaRoot 'include\lua.h')) -and
    (Test-Install $Lua @('-e', 'io.write(_VERSION)') 'Lua 5.4')
if (-not $luaReady) {
    Write-Host "building Lua $LuaVersion"
    $staging = New-Staging
    & tar -xzf (Get-Download $Downloads[1]) -C $staging
    if ($LASTEXITCODE -ne 0) {
        throw "tar could not unpack the Lua source (exit $LASTEXITCODE); tar ships with Windows 10 1803 and later"
    }
    $source = Join-Path $staging "lua-$LuaVersion\src"
    $build = Join-Path $staging 'build'
    New-Item -ItemType Directory -Force -Path $build | Out-Null
    $modules = @(Get-ChildItem -Path $source -Filter *.c |
        Where-Object { $_.Name -notin 'lua.c', 'luac.c', 'onelua.c' } |
        ForEach-Object { $_.FullName })
    Push-Location $build
    try {
        & $GCC -O2 -DLUA_BUILD_AS_DLL -shared -o lua54.dll @modules '-Wl,--out-implib,lua54.lib'
        if ($LASTEXITCODE -ne 0) {
            throw "compiling lua54.dll failed (exit $LASTEXITCODE)"
        }
        & $GCC -O2 -o lua.exe (Join-Path $source 'lua.c') -L. -llua54
        if ($LASTEXITCODE -ne 0) {
            throw "compiling lua.exe failed (exit $LASTEXITCODE)"
        }
        & $Strip --strip-unneeded lua54.dll lua.exe
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "strip exited $LASTEXITCODE; the build is fine, the binaries are only larger"
        }
    } finally {
        Pop-Location
    }
    $built = & (Join-Path $build 'lua.exe') -e 'io.write(_VERSION)'
    if ($LASTEXITCODE -ne 0 -or $built -ne 'Lua 5.4') {
        throw "the freshly built interpreter reports '$built', not Lua 5.4"
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $LuaRoot 'bin'), (Join-Path $LuaRoot 'lib'),
        (Join-Path $LuaRoot 'include') | Out-Null
    Copy-Item (Join-Path $build 'lua.exe') $Lua -Force
    Copy-Item (Join-Path $build 'lua54.dll') (Join-Path $LuaRoot 'bin\lua54.dll') -Force
    Copy-Item (Join-Path $build 'lua54.lib') (Join-Path $LuaRoot 'lib\lua54.lib') -Force
    foreach ($header in 'lua.h', 'luaconf.h', 'lualib.h', 'lauxlib.h') {
        Copy-Item (Join-Path $source $header) (Join-Path $LuaRoot "include\$header") -Force
    }
    Remove-Item $staging -Recurse -Force
}

# 3. LuaRocks. The standalone Windows build bundles its own Lua 5.4.
if (-not (Test-Install $LuaRocks @('--version') $LuaRocksVersion)) {
    Write-Host "installing LuaRocks $LuaRocksVersion"
    $staging = New-Staging
    Expand-Archive -Path (Get-Download $Downloads[2]) -DestinationPath $staging -Force
    $bundle = Get-ChildItem -Path $staging -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $LuaRocksRoot | Out-Null
    Copy-Item (Join-Path $bundle.FullName 'luarocks.exe') $LuaRocks -Force
    Copy-Item (Join-Path $bundle.FullName 'luarocks-admin.exe') (Join-Path $LuaRocksRoot 'luarocks-admin.exe') -Force
    Remove-Item $staging -Recurse -Force
}

# 4. LuaRocks reads its config from the deploy tree, so it lives there.
$LuaDir = $LuaRoot -replace '\\', '/'
$config = @"
lua_interpreter = "lua"

variables = {
   LUA_DIR = "$LuaDir",
   LUA_BINDIR = "$LuaDir/bin",
   LUA_INCDIR = "$LuaDir/include",
   LUA_LIBDIR = "$LuaDir/lib",
   LUALIB = "lua54.lib"
}
"@
New-Item -ItemType Directory -Force -Path $DeployTree | Out-Null
Set-Content -Path (Join-Path $DeployTree 'config-5.4.lua') -Value $config -Encoding ASCII

# 5. The rocks a Lua suite reports through, in the order setup-ubuntu installs
#    them. luafilesystem is named explicitly because LuaCov loads lfs directly,
#    before Busted resolves anything.
$env:PATH = "$(Join-Path $MinGWRoot 'bin');$(Join-Path $LuaRoot 'bin');$LuaRocksRoot;$env:PATH"
& $LuaRocks --lua-version=5.4 install luafilesystem
Assert-Exit 'luarocks install luafilesystem' $LASTEXITCODE
& $LuaRocks --lua-version=5.4 install busted
Assert-Exit 'luarocks install busted' $LASTEXITCODE
& $LuaRocks --lua-version=5.4 install luacov
Assert-Exit 'luarocks install luacov' $LASTEXITCODE
& $LuaRocks --lua-version=5.4 install cluacov 1.0.0-1
Assert-Exit 'luarocks install cluacov 1.0.0-1' $LASTEXITCODE

# 6. The lcov reporter goes to the user tree and nowhere else, as on Ubuntu, so
#    both platforms have one shape; a deploy-tree copy is deleted first. Not
#    being installed is the normal case on a fresh machine, and Windows
#    PowerShell 5.1 turns a redirected stderr line into a terminating error, so
#    this one may fail.
try {
    & $LuaRocks --lua-version=5.4 remove --force --deps-mode none luacov-reporter-lcov 2>&1 | Out-Null
} catch {
    Write-Verbose 'no deploy-tree luacov-reporter-lcov to remove'
}
$shadow = Join-Path $DeployTree 'share\lua\5.4\luacov\reporter\lcov.lua'
if (Test-Path $shadow) {
    Remove-Item $shadow -Force
}
& $LuaRocks --lua-version=5.4 --tree $UserTree install --deps-mode none luacov-reporter-lcov
Assert-Exit 'luarocks install luacov-reporter-lcov' $LASTEXITCODE

# 7. Persist the search paths. LuaRocks' tree is not on Lua's default path, so
#    require('busted.runner') fails without LUA_PATH even where busted is
#    installed. The trailing ";;" keeps Lua's built-in default path as the
#    fallback.
$LuaPath = './?.lua;./?/init.lua;' +
    "$DeployTree\share\lua\5.4\?.lua;$DeployTree\share\lua\5.4\?/init.lua;" +
    "$UserTree\share\lua\5.4\?.lua;$UserTree\share\lua\5.4\?/init.lua;;"
$LuaCPath = './?.dll;' +
    "$DeployTree\lib\lua\5.4\?.dll;$UserTree\lib\lua\5.4\?.dll;;"
$PathEntries = @((Join-Path $LuaRoot 'bin'), $LuaRocksRoot, (Join-Path $DeployTree 'bin'))

if (-not $SkipUserEnvironment) {
    # [Environment] and not setx: setx truncates at 1024 characters.
    $existing = @(([Environment]::GetEnvironmentVariable('PATH', 'User') -split ';') | Where-Object { $_ })
    $merged = @($PathEntries | Where-Object { $existing -notcontains $_ }) + $existing
    [Environment]::SetEnvironmentVariable('PATH', ($merged -join ';'), 'User')
    [Environment]::SetEnvironmentVariable('LUA_PATH', $LuaPath, 'User')
    [Environment]::SetEnvironmentVariable('LUA_CPATH', $LuaCPath, 'User')
}
$env:PATH = (@($PathEntries | Where-Object { ($env:PATH -split ';') -notcontains $_ }) + ($env:PATH -split ';')) -join ';'
$env:LUA_PATH = $LuaPath
$env:LUA_CPATH = $LuaCPath

# GitHub Actions: a later step starts from the job's environment, not this
# process's, so the same three values go through the step environment file.
if ($env:GITHUB_ENV) {
    Add-Content -Path $env:GITHUB_ENV -Value "PATH=$env:PATH"
    Add-Content -Path $env:GITHUB_ENV -Value "LUA_PATH=$LuaPath"
    Add-Content -Path $env:GITHUB_ENV -Value "LUA_CPATH=$LuaCPath"
}

# 8. Prove it rather than report it: the interpreter loads the modules the tests
#    need, and PATH reaches that interpreter. A machine-wide PATH is searched
#    after the user one, so an older lua.exe there cannot outrank this one — but
#    one already on the machine PATH is searched before it, and the Lua cases
#    would fail rather than skip.
$resolvedLua = (Get-Command lua -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1).Source
if ($resolvedLua -and $resolvedLua -ne $Lua) {
    throw "PATH resolves lua to $resolvedLua, not $Lua; remove that entry, or put $LuaRoot\bin ahead of it"
}
& $Lua -e "local version = _VERSION; assert(version == 'Lua 5.4', version); print('lua      ' .. version)"
Assert-Exit 'lua -e _VERSION' $LASTEXITCODE
& $Lua -e "local version = require('cluacov.version'); assert(version == '1.0.0', version); print('cluacov  ' .. version)"
Assert-Exit 'lua -e cluacov.version' $LASTEXITCODE
& $Lua -e "assert(require('lfs')); assert(require('luacov.runner')); assert(require('busted.runner')); print('modules  lfs, luacov.runner, busted.runner')"
Assert-Exit 'lua -e modules' $LASTEXITCODE
$reporterPath = & $Lua -e "io.write(package.searchpath('luacov.reporter.lcov', package.path) or '')"
Assert-Exit 'lua -e searchpath' $LASTEXITCODE
if (-not $reporterPath) {
    throw 'luacov-reporter-lcov is not reachable on LUA_PATH'
}
if (-not ($reporterPath -like "$UserTree*")) {
    throw "luacov-reporter-lcov resolved to $reporterPath, which is not the user tree $UserTree"
}
Write-Host "lcov     $reporterPath"
& $LuaRocks --version | Select-Object -First 1
Assert-Exit 'luarocks --version' $LASTEXITCODE
& (Get-Command busted).Source --version
Assert-Exit 'busted --version' $LASTEXITCODE

Write-Host ''
Write-Host 'Done. A shell started now sees lua, luarocks, busted and luacov.'
Write-Host 'Then: python -m pytest (from the mutator root, with a .venv) — the Lua mutation cases now run instead of skipping'
