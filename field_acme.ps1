param(
  [Parameter(Mandatory=$true)][string]$Identifier,
  [Parameter(Mandatory=$true)][string]$CertPath,
  [Parameter(Mandatory=$true)][string]$KeyPath,
  [Parameter(Mandatory=$true)][string]$AccountKeyPath,
  [int]$ChallengePort = 8080,
  [string]$DirectoryUrl = 'https://acme-v02.api.letsencrypt.org/directory'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function B64Url([byte[]]$Bytes) {
  return [Convert]::ToBase64String($Bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
}
function Utf8([string]$Text) { return [Text.Encoding]::UTF8.GetBytes($Text) }
function Json($Value) { return ($Value | ConvertTo-Json -Compress -Depth 12) }
function Pem([string]$Label, [byte[]]$Bytes) {
  $b64 = [Convert]::ToBase64String($Bytes)
  $lines = for ($i=0; $i -lt $b64.Length; $i+=64) { $b64.Substring($i, [Math]::Min(64,$b64.Length-$i)) }
  return "-----BEGIN $Label-----`n" + ($lines -join "`n") + "`n-----END $Label-----`n"
}
function Import-Pkcs8Rsa([string]$Path) {
  $text = Get-Content -Raw $Path
  $raw = $text -replace '-----BEGIN PRIVATE KEY-----','' -replace '-----END PRIVATE KEY-----','' -replace '\s',''
  $bytes = [Convert]::FromBase64String($raw)
  $rsa = [Security.Cryptography.RSA]::Create()
  $read = 0
  $rsa.ImportPkcs8PrivateKey($bytes, [ref]$read)
  return $rsa
}
function Get-Nonce([string]$Url) {
  $r = Invoke-WebRequest -UseBasicParsing -Method Head $Url
  return [string]$r.Headers['Replay-Nonce']
}

$ip = $null
if (-not [Net.IPAddress]::TryParse($Identifier, [ref]$ip)) { throw 'Identifier must be an IP address' }
$dir = Invoke-RestMethod -UseBasicParsing $DirectoryUrl
if (-not $dir.newNonce -or -not $dir.newAccount -or -not $dir.newOrder) { throw 'invalid ACME directory' }

$account = if (Test-Path $AccountKeyPath) { Import-Pkcs8Rsa $AccountKeyPath } else { [Security.Cryptography.RSA]::Create(2048) }
if (-not (Test-Path $AccountKeyPath)) {
  New-Item -ItemType Directory -Force ([IO.Path]::GetDirectoryName($AccountKeyPath)) | Out-Null
  [IO.File]::WriteAllText($AccountKeyPath, (Pem 'PRIVATE KEY' $account.ExportPkcs8PrivateKey()), [Text.Encoding]::ASCII)
}
$ap = $account.ExportParameters($false)
$jwk = [ordered]@{ e=(B64Url $ap.Exponent); kty='RSA'; n=(B64Url $ap.Modulus) }
$jwkJson = Json $jwk
$thumbBytes = [Security.Cryptography.SHA256]::HashData((Utf8 $jwkJson))
$thumbprint = B64Url $thumbBytes
$nonce = Get-Nonce $dir.newNonce
$kid = $null

function Invoke-Acme([string]$Url, $Payload, [switch]$PostAsGet, [switch]$UseJwk) {
  $script:nonce = if ($script:nonce) { $script:nonce } else { Get-Nonce $dir.newNonce }
  $protected = [ordered]@{ alg='RS256'; nonce=$script:nonce; url=$Url }
  if ($UseJwk -or -not $script:kid) { $protected.jwk = $jwk } else { $protected.kid = $script:kid }
  $p64 = B64Url (Utf8 (Json $protected))
  $payload64 = if ($PostAsGet) { '' } else { B64Url (Utf8 (Json $Payload)) }
  $toSign = Utf8 ($p64 + '.' + $payload64)
  $sig = $account.SignData($toSign, [Security.Cryptography.HashAlgorithmName]::SHA256, [Security.Cryptography.RSASignaturePadding]::Pkcs1)
  $body = Json ([ordered]@{ protected=$p64; payload=$payload64; signature=(B64Url $sig) })
  try {
    $r = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $Url -ContentType 'application/jose+json' -Body $body
  } catch {
    $resp = $_.Exception.Response
    if ($resp -and $resp.Headers['Replay-Nonce']) { $script:nonce = [string]$resp.Headers['Replay-Nonce'] }
    throw
  }
  $script:nonce = [string]$r.Headers['Replay-Nonce']
  return $r
}

$acctResp = Invoke-Acme $dir.newAccount ([ordered]@{termsOfServiceAgreed=$true}) -UseJwk
$kid = [string]$acctResp.Headers['Location']
if (-not $kid) { throw 'ACME account location missing' }
$orderResp = Invoke-Acme $dir.newOrder ([ordered]@{identifiers=@([ordered]@{type='ip';value=$Identifier})})
$orderUrl = [string]$orderResp.Headers['Location']
$order = $orderResp.Content | ConvertFrom-Json
if (-not $orderUrl -or -not $order.authorizations -or -not $order.finalize) { throw 'invalid ACME order' }

$authResp = Invoke-Acme ([string]$order.authorizations[0]) $null -PostAsGet
$auth = $authResp.Content | ConvertFrom-Json
$challenge = @($auth.challenges | Where-Object {$_.type -eq 'http-01'})[0]
if (-not $challenge.token -or -not $challenge.url) { throw 'http-01 unavailable for IP identifier' }
$token = [string]$challenge.token
$keyAuthorization = $token + '.' + $thumbprint

# Deliberately tiny one-purpose HTTP-01 listener. Router mapping is handled by
# native_aperture.py so this process never needs a hosted tunnel or rendezvous.
$job = Start-Job -ArgumentList $ChallengePort,$token,$keyAuthorization -ScriptBlock {
  param($Port,$Token,$Value)
  $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any,[int]$Port)
  $listener.Start()
  $deadline = [DateTime]::UtcNow.AddMinutes(4)
  try {
    while ([DateTime]::UtcNow -lt $deadline) {
      if (-not $listener.Pending()) { Start-Sleep -Milliseconds 20; continue }
      $client = $listener.AcceptTcpClient()
      try {
        $stream = $client.GetStream(); $reader = New-Object IO.StreamReader($stream,[Text.Encoding]::ASCII,$false,1024,$true)
        $line = $reader.ReadLine(); while (($h=$reader.ReadLine()) -ne '') { if ($null -eq $h) { break } }
        $path = if ($line -match '^GET\s+([^\s]+)') { $Matches[1] } else { '' }
        if ($path -eq ('/.well-known/acme-challenge/' + $Token)) {
          $body = [Text.Encoding]::ASCII.GetBytes($Value); $status='200 OK'
        } else { $body=[Text.Encoding]::ASCII.GetBytes('not found'); $status='404 Not Found' }
        $head = [Text.Encoding]::ASCII.GetBytes("HTTP/1.1 $status`r`nContent-Type: text/plain`r`nContent-Length: $($body.Length)`r`nConnection: close`r`n`r`n")
        $stream.Write($head,0,$head.Length); $stream.Write($body,0,$body.Length); $stream.Flush()
      } finally { $client.Dispose() }
    }
  } finally { $listener.Stop() }
}
try {
  Start-Sleep -Milliseconds 120
  $null = Invoke-Acme ([string]$challenge.url) ([ordered]@{})
  $authUrl = [string]$order.authorizations[0]
  $valid = $false
  for ($i=0; $i -lt 60; $i++) {
    Start-Sleep -Milliseconds ([Math]::Min(2000, 250 + $i*60))
    $poll = Invoke-Acme $authUrl $null -PostAsGet
    $state = $poll.Content | ConvertFrom-Json
    if ($state.status -eq 'valid') { $valid=$true; break }
    if ($state.status -eq 'invalid') { throw ('ACME authorization invalid: ' + (Json $state)) }
  }
  if (-not $valid) { throw 'ACME authorization timed out' }
} finally {
  Stop-Job $job -ErrorAction SilentlyContinue | Out-Null
  Remove-Job $job -Force -ErrorAction SilentlyContinue
}

$leaf = [Security.Cryptography.RSA]::Create(2048)
$req = [Security.Cryptography.X509Certificates.CertificateRequest]::new(
  ('CN=' + $Identifier), $leaf, [Security.Cryptography.HashAlgorithmName]::SHA256,
  [Security.Cryptography.RSASignaturePadding]::Pkcs1
)
$san = [Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
$san.AddIpAddress($ip)
$req.CertificateExtensions.Add($san.Build())
$csr = $req.CreateSigningRequest()
$null = Invoke-Acme ([string]$order.finalize) ([ordered]@{csr=(B64Url $csr)})

$certificateUrl = $null
for ($i=0; $i -lt 70; $i++) {
  Start-Sleep -Milliseconds ([Math]::Min(2000, 250 + $i*60))
  $poll = Invoke-Acme $orderUrl $null -PostAsGet
  $state = $poll.Content | ConvertFrom-Json
  if ($state.status -eq 'valid' -and $state.certificate) { $certificateUrl=[string]$state.certificate; break }
  if ($state.status -eq 'invalid') { throw ('ACME order invalid: ' + (Json $state)) }
}
if (-not $certificateUrl) { throw 'ACME certificate URL timed out' }
$certResp = Invoke-Acme $certificateUrl $null -PostAsGet
$certPem = [string]$certResp.Content
if ($certPem -notmatch 'BEGIN CERTIFICATE') { throw 'ACME certificate payload invalid' }
New-Item -ItemType Directory -Force ([IO.Path]::GetDirectoryName($CertPath)) | Out-Null
[IO.File]::WriteAllText($CertPath, $certPem, [Text.Encoding]::ASCII)
[IO.File]::WriteAllText($KeyPath, (Pem 'PRIVATE KEY' $leaf.ExportPkcs8PrivateKey()), [Text.Encoding]::ASCII)

[ordered]@{
  ok=$true
  identifier=$Identifier
  cert=$CertPath
  key=$KeyPath
  issued_at=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
  # Short-lived profile is intentional; renewal is automated by the resident.
  renewal_class='short-lived-ip'
} | ConvertTo-Json -Compress
