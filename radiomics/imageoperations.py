import SimpleITK as sitk
import numpy, pywt, logging
from itertools import chain

def getHistogram(binwidth, parameterValues):
  # Start binning form the first value lesser than or equal to the minimum value and evenly dividable by binwidth
  lowBound = min(parameterValues) - (min(parameterValues) % binwidth)
  # Add + binwidth to ensure the maximum value is included in the range generated by numpu.arange
  highBound = max(parameterValues) + binwidth

  binedges = numpy.arange(lowBound, highBound, binwidth)

  binedges[-1] += 1 # ensures that max(self.targertVoxelArray) is binned to upper bin by numpy.digitize

  return numpy.histogram(parameterValues, bins=binedges)

def binImage(binwidth, parameterValues, parameterMatrix = None, parameterMatrixCoordinates = None):
  histogram = getHistogram(binwidth, parameterValues)
  parameterMatrix[parameterMatrixCoordinates] = numpy.digitize(parameterValues, histogram[1])

  return parameterMatrix, histogram

def cropToTumorMask(imageNode, maskNode):
  """
  Create a sitkImage of the segmented region of the image based on the input label.

  Create a sitkImage of the labelled region of the image, cropped to have a
  cuboid shape equal to the ijk boundaries of the label.

  Returns both the cropped version of the image and the cropped version of the labelmap.
  """

  oldMaskID = maskNode.GetPixelID()
  maskNode = sitk.Cast(maskNode, sitk.sitkInt32)
  size = numpy.array(maskNode.GetSize())

  #Determine bounds
  lsif = sitk.LabelStatisticsImageFilter()
  lsif.Execute(imageNode, maskNode)
  bb = lsif.GetBoundingBox(1)

  ijkMinBounds = bb[0::2]
  ijkMaxBounds = size - bb[1::2]

  #Crop Image
  cif = sitk.CropImageFilter()
  cif.SetLowerBoundaryCropSize(ijkMinBounds)
  cif.SetUpperBoundaryCropSize(ijkMaxBounds)
  croppedImageNode = cif.Execute(imageNode)
  croppedMaskNode = cif.Execute(maskNode)

  croppedMaskNode = sitk.Cast(croppedMaskNode, oldMaskID)

  return croppedImageNode, croppedMaskNode

def resampleImage(imageNode, maskNode, resampledPixelSpacing, interpolator=sitk.sitkBSpline, padDistance= 0):
  """Resamples image or label to the specified pixel spacing (The default interpolator is Bspline)

  'imageNode' is a SimpleITK Object, and 'resampledPixelSpacing' is the output pixel spacing.
  Enumerator references for interpolator:
  0 - sitkNearestNeighbor
  1 - sitkLinear
  2 - sitkBSpline
  3 - sitkGaussian
  """

  if imageNode == None or maskNode == None:
    return None

  oldSpacing = numpy.array(imageNode.GetSpacing())

  # If current spacing is equal to resampledPixelSpacing, no interpolation is needed,
  # crop/pad image using cropTumorMaskToCube
  if numpy.array_equal(oldSpacing, resampledPixelSpacing):
    return cropTumorMaskToCube(imageNode, maskNode, padDistance)

  # Determine bounds of cropped volume
  labelNodeArray = sitk.GetArrayFromImage(maskNode)
  targetVoxelsCoordinates = numpy.where(labelNodeArray != 0)
  ijkMinBounds = numpy.min(targetVoxelsCoordinates, 1) - padDistance
  ijkMaxBounds = numpy.max(targetVoxelsCoordinates, 1) + padDistance

  oldSize =  ijkMaxBounds-ijkMinBounds + 1  # size of the cropped and padded tumorvolume

  # Recalculate the new size. Round up to prevent data loss.
  newSize = numpy.array(numpy.ceil(oldSize * oldSpacing / resampledPixelSpacing),dtype='int')
  # Origin is located in center of first voxel, e.g. 1/2 of the spacing
  # from Corner, which corresponds to 0 in the original Index coordinate space.
  # The new spacing will be in 0 the new Index coordinate space. Here we use continuous
  # index to calculate where the new 0 of the new Index coordinate space (of the original volume
  # in terms of the original spacing, and add the minimum bounds of the cropped area to
  # get the new Index coordinate space of the cropped volume in terms of the original spacing.
  # Then use the ITK functionality to bring the contiuous index into the physical space (mm)
  newOriginIndex = numpy.array(.5*(resampledPixelSpacing-oldSpacing)/oldSpacing)
  newCroppedOriginIndex = newOriginIndex + numpy.array(ijkMinBounds[::-1])
  newOrigin = imageNode.TransformContinuousIndexToPhysicalPoint(newCroppedOriginIndex)

  oldImagePixelType = imageNode.GetPixelID()
  oldMaskPixelType = maskNode.GetPixelID()

  imageDirection = numpy.array(imageNode.GetDirection())

  rif = sitk.ResampleImageFilter()

  rif.SetOutputSpacing(resampledPixelSpacing)
  rif.SetOutputDirection(imageDirection)
  rif.SetSize(newSize)
  rif.SetOutputOrigin(newOrigin)

  rif.SetOutputPixelType(oldImagePixelType)
  rif.SetInterpolator(interpolator)
  resampledImageNode = rif.Execute(imageNode)

  rif.SetOutputPixelType(oldMaskPixelType)
  rif.SetInterpolator(sitk.sitkNearestNeighbor)
  resampledMaskNode = rif.Execute(maskNode)

  return resampledImageNode,resampledMaskNode

#
# Use the SimpleITK LaplacianRecursiveGaussianImageFilter
# on the input image with the given sigmaValue and return
# the filtered image.
# If sigmaValue is not greater than zero, return the input image.
#
def applyLoG(inputImage, sigmaValue=0.5):
  if sigmaValue > 0.0:
    lrgif = sitk.LaplacianRecursiveGaussianImageFilter()
    lrgif.SetNormalizeAcrossScale(True)
    lrgif.SetSigma(sigmaValue)
    return lrgif.Execute(inputImage)
  else:
    logging.info('applyLoG: sigma must be greater than 0.0: %g', sigmaValue)
    return inputImage

def applyThreshold(inputImage, lowerThreshold, upperThreshold, insideValue=None, outsideValue=0):
  # this mode is useful to generate the mask of thresholded voxels
  if insideValue:
    tif = sitk.BinaryThresholdImageFilter()
    tif.SetInsideValue(insideValue)
    tif.SetLowerThreshold(lowerThreshold)
    tif.SetUpperThreshold(upperThreshold)
  else:
    tif = sitk.ThresholdImageFilter()
    tif.SetLower(lowerThreshold)
    tif.SetUpper(upperThreshold)
  tif.SetOutsideValue(outsideValue)
  return tif.Execute(inputImage)

def swt3(inputImage, wavelet="coif1", level=1, start_level=0):
  matrix = sitk.GetArrayFromImage(inputImage)
  matrix = numpy.asarray(matrix)
  data = matrix.copy()
  if data.ndim != 3:
    raise ValueError("Expected 3D data array")

  original_shape = matrix.shape
  adjusted_shape = tuple([dim+1 if dim % 2 != 0 else dim for dim in original_shape])
  data = numpy.resize(data, adjusted_shape)

  if not isinstance(wavelet, pywt.Wavelet):
    wavelet = pywt.Wavelet(wavelet)

  for i in range(0, start_level):
    H, L = decompose_i(data, wavelet)
    LH, LL = decompose_j(L, wavelet)
    LLH, LLL = decompose_k(LL, wavelet)

    data = LLL.copy()

  ret = []
  for i in range(start_level, start_level + level):
    H, L = decompose_i(data, wavelet)

    HH, HL = decompose_j(H, wavelet)
    LH, LL = decompose_j(L, wavelet)

    HHH, HHL = decompose_k(HH, wavelet)
    HLH, HLL = decompose_k(HL, wavelet)
    LHH, LHL = decompose_k(LH, wavelet)
    LLH, LLL = decompose_k(LL, wavelet)

    data = LLL.copy()

    dec = {'HHH': HHH,
           'HHL': HHL,
           'HLH': HLH,
           'HLL': HLL,
           'LHH': LHH,
           'LHL': LHL,
           'LLH': LLH}
    for decName, decImage in dec.iteritems():
      decTemp = decImage.copy()
      decTemp= numpy.resize(decTemp, original_shape)
      sitkImage = sitk.GetImageFromArray(decTemp)
      sitkImage.CopyInformation(inputImage)
      dec[decName] = sitkImage

    ret.append(dec)

  data= numpy.resize(data, original_shape)
  approximation = sitk.GetImageFromArray(data)
  approximation.CopyInformation(inputImage)

  return approximation, ret

def decompose_i(data, wavelet):
  #process in i:
  H, L = [], []
  i_arrays = chain.from_iterable(numpy.transpose(data,(0,1,2)))
  for i_array in i_arrays:
    cA, cD = pywt.swt(i_array, wavelet, level=1, start_level=0)[0]
    H.append(cD)
    L.append(cA)
  H = numpy.hstack(H).reshape(data.shape)
  L = numpy.hstack(L).reshape(data.shape)
  return H, L

def decompose_j(data, wavelet):
  #process in j:
  H, L = [], []
  j_arrays = chain.from_iterable(numpy.transpose(data,(0,1,2)))
  for j_array in j_arrays:
    cA, cD = pywt.swt(j_array, wavelet, level=1, start_level=0)[0]
    H.append(cD)
    L.append(cA)
  H = numpy.asarray( [slice.T for slice in numpy.split(numpy.vstack(H), data.shape[0])] )
  L = numpy.asarray( [slice.T for slice in numpy.split(numpy.vstack(L), data.shape[0])] )
  return H, L

def decompose_k(data, wavelet):
  #process in k:
  H, L = [], []
  k_arrays = chain.from_iterable(numpy.transpose(data,(1,2,0)))
  for k_array in k_arrays:
    cA, cD = pywt.swt(k_array, wavelet, level=1, start_level=0)[0]
    H.append(cD)
    L.append(cA)
  H = numpy.dstack(H).reshape(data.shape)
  L = numpy.dstack(L).reshape(data.shape)
  return H, L
